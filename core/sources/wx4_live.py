"""wxcsm 数据源接入:直接读本机微信 4.x(Windows)加密库的适配器。

设计:
  1) detect_wechat_env() 找账号与加密库;
  2) 密钥来源优先级:keyring 缓存 → 当前运行中微信的只读内存抓取
     (memkey.capture_key,被动等待/轮询,无注入、无重启);
  3) 把加密库只读复制到 %USERPROFILE%\\.wxcsm\\wx4_staging\\<account>\\,
     decrypt_db_file 逐页解密为明文 sqlite;
  4) 扫描明文库得到联系人/消息(4.x 结构:Msg_<md5> 消息表 + contact 库联系人)。

对外行为与其它 ChatSource 完全一致:list_contacts() / fetch(contact, start, end)。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..config import CONFIG_DIR
from ..models import Contact, Message
from ..wx4 import cipher, keyring, locate
from ..wx4.errors import KeyNotFoundError, WechatNotRunningError, Wx4Error
from ..sources.base import ChatSource, SourceError

STAGING_ROOT = CONFIG_DIR / "wx4_staging"

# 4.x 消息类型 → 占位说明(与 3.x 语义一致)
TYPE_MAP = {
    1: "text", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
    42: "名片", 48: "位置", 49: "文件/链接", 10000: "系统消息", 10002: "系统消息",
}

# ZSTD 压缩魔数
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

try:
    import zstandard as zstd
    _ZSTD_CTX = zstd.ZstdDecompressor()
    _HAVE_ZSTD = True
except ImportError:
    _HAVE_ZSTD = False


class Wx4LiveSource(ChatSource):
    key = "wx4"
    label = "直接读取本机微信（Windows 4.x）聊天记录"
    hint = ("自动探测本机已登录的微信 4.x 账号并读取加密聊天库。\n"
            "首次使用需在微信保持登录时抓取一次数据库密钥（只读内存扫描，无注入、无重启、无网络）。")
    needs_path = False
    path_kind = "无"

    def __init__(self, **options):
        super().__init__(**options)
        self._account: Optional[locate.WxAccount] = None
        self._key_hex: Optional[str] = None
        self._staged: Optional[Path] = None
        self._contacts: List[Contact] = []
        self._scanned = False
        # 可选的进度回调(由 GUI/CLI 注入, 用于展示取钥/解密进度); 默认静默
        self._progress_cb: Optional[Callable[[str], None]] = options.get("progress_cb")

    def _echo(self, msg: str) -> None:
        if self._progress_cb:
            try:
                self._progress_cb(msg)
            except Exception:
                pass

    # ---------------- 环境与密钥 ----------------
    def _resolve_account(self) -> locate.WxAccount:
        from ..wx4 import locate as _loc
        # 关键:微信运行时环境可能已切换账号(detect 有 60s 缓存),故强制刷新,
        # 以反映"当前正在登录的账号",否则换账号后仍会读到旧账号。
        running = bool(_loc.running_weixin_pids())
        env = _loc.detect_wechat_env(force=running)
        if not env.accounts:
            raise SourceError(
                "没有在本机找到已登录的微信 4.x 数据目录。\n"
                "请确认:① 电脑上装的是微信 4.x 并登录过;② 数据目录未被移动。\n"
                "若无法自动定位,请改用「导入聊天记录文件」数据源。")
        if running:
            # 微信运行中 -> 选当前活跃(正在登录)账号。env.accounts 已按活跃度排序,
            # 首位即最近被微信写入数据的账号 = 正在登录的账号;它的密钥在内存里可扫取。
            self._account = env.accounts[0]
            self._echo(f"选用账号 {self._account.account}")
            return self._account
        # 微信未运行(离线) -> 优先有缓存密钥的账号,才能离线解密。
        for a in env.accounts:
            if keyring.has_key(a.account):
                self._account = a
                self._echo(f"离线模式选用有缓存的账号 {a.account}")
                return a
        self._account = env.accounts[0]
        self._echo(f"选用账号 {self._account.account}")
        return self._account

    def _obtain_key(self) -> str:
        """取目标账号的密钥:优先用该账号缓存(离线可读);否则 V4 只读内存扫描。

        目标账号由 _resolve_account 决定:
          - 微信运行中 -> 当前活跃(正在登录)账号,实时扫描取它的密钥;
          - 微信未运行 -> 有缓存密钥的账号(离线解密)。
        这样换账号登录后会自动锁定新账号,不会误用旧账号的缓存。
        """
        from ..wx4 import locate as _locate, memkey
        from ..wx4.errors import WechatNotRunningError, Wx4Error

        acc = self._resolve_account()   # 已按"运行→活跃 / 离线→缓存"原则选定

        # 目标账号已缓存 -> 直接用(离线也成立)
        c = keyring.load_key(acc.account)
        if c and c.get("db_key"):
            self._echo(f"使用已缓存密钥(账号 {acc.account})。")
            return c["db_key"]

        # 无缓存 -> 需 V4 只读内存扫描;微信未运行则内存里不可能有该账号密钥。
        if not _locate.running_weixin_pids():
            raise KeyNotFoundError(
                f"账号 {acc.account} 尚未缓存密钥, 且微信当前未在运行。\n"
                "请先打开并登录微信(保持登录态), 再点「加载聊天对象」自动抓取。")

        self._echo(f"账号 {acc.account} 无缓存密钥, 正在只读扫描微信进程内存…")
        try:
            key = memkey.capture_key(
                acc,
                progress_cb=self._echo,
                restart=False,
                timeout=180,
            )
        except (KeyNotFoundError, WechatNotRunningError, Wx4Error) as e:
            raise KeyNotFoundError(
                f"通过只读扫描未能取得账号 {acc.account} 的密钥。\n"
                f"原因: {e}\n"
                "请确认该账号确为微信当前登录(活跃)的账号后重试。")
        except Exception as e:
            raise KeyNotFoundError(f"扫描密钥出现异常: {e}")
        if not key:
            raise KeyNotFoundError(
                f"未能在内存中找到账号 {acc.account} 的密钥。\n"
                "请确认该账号是微信当前登录的活跃账号(其密钥才会在内存里)。")

        self._account = acc
        self._key_hex = key
        try:
            keyring.save_key(acc.account, key)
        except Exception:
            pass  # 缓存失败不阻断流程
        self._echo(f"已通过只读扫描取得并缓存账号 {acc.account} 的密钥。")
        return key

    # ---------------- 暂存与解密 ----------------
    def _stage_and_decrypt(self) -> Path:
        """把账号的加密库复制到 staging 并解密,返回明文库目录。"""
        acc = self._resolve_account()
        if self._staged and self._staged.exists():
            return self._staged
        key_hex = self._key_hex or self._obtain_key()

        # 只读复制(微信开着也能共享读;更一致的结果建议先退出微信)
        out_dir = STAGING_ROOT / acc.account
        out_dir.mkdir(parents=True, exist_ok=True)
        src_dbs: List[Path] = list(acc.message_dbs)
        if acc.contact_db:
            src_dbs.append(acc.contact_db)
        if acc.session_db:
            src_dbs.append(acc.session_db)
        if not src_dbs:
            raise SourceError("该账号下没有找到 message/contact 数据库文件。")

        # 幂等复用: 若 staging 中对应文件已是明文(非加密库头), 跳过复制与解密,
        # 使重复运行(list_contacts / fetch / 二次导出)不必每次重蹈 20-30s 全量解密.
        changed = False
        for src in src_dbs:
            rel = src.relative_to(acc.db_storage)
            dst = out_dir / rel
            if dst.exists() and _is_decrypted(dst):
                continue  # 已解密复用
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 共享读:微信可能仍持有该文件
                with open(src, "rb", buffering=0) as fin:
                    dst.write_bytes(fin.read())
            except PermissionError:
                raise SourceError(
                    f"无法读取数据库文件(可能正被微信独占):{src}\n"
                    "请退出微信后重试。")
            changed = True

        # 逐个解密:加密文件就地替换为明文(仅操作 staging 副本)
        for p in out_dir.rglob("*.db"):
            if _is_decrypted(p):
                continue
            tmp = p.with_suffix(".db.tmp")
            try:
                cipher.decrypt_db_file(str(p), key_hex, str(tmp), check_hmac=True)
                tmp.replace(p)
            except Exception as e:
                if p.name in {"contact.db", "session.db"} or p.name.startswith("message_"):
                    raise SourceError(f"解密失败:{p.name}({e})")
        self._staged = out_dir
        return out_dir

    # ---------------- 健康检查 ----------------
    def health(self) -> Tuple[bool, str]:
        try:
            acc = self._resolve_account()
        except SourceError as e:
            return False, str(e)
        if not keyring.has_key(acc.account) and not locate.running_weixin_pids():
            return False, (
                f"已找到账号 {acc.account},但尚未抓取数据库密钥,且微信当前未运行。\n"
                "请先打开并登录微信(登录态),再点「加载聊天对象」自动抓取。")
        return True, f"已识别账号 {acc.account}({len(acc.message_dbs)} 个消息库)"

    # ---------------- 联系人 ----------------
    def list_contacts(self) -> List[Contact]:
        if self._scanned:
            return self._contacts
        key_hex = self._key_hex or self._obtain_key()
        self._key_hex = key_hex
        root = self._stage_and_decrypt()
        self._contacts = self._scan(root)
        self._scanned = True
        return self._contacts

    def _scan(self, root: Path) -> List[Contact]:
        """扫描明文库:联系人 + 会话统计。微信4.x 结构首次解密后由探查确定,
        这里按「尽力而为」:优先从 contact 类表取联系人(避免遍历 132 张消息表)。"""
        contacts: Dict[str, Contact] = {}
        for db in sorted(root.rglob("*.db")):
            try:
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
            except sqlite3.Error:
                continue
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                # 粗筛: 只对明显是联系人/好友的表做结构探查, 跳过海量消息表
                for tb in tables:
                    tlow = tb.lower()
                    if tlow not in ("contact", "wacontact", "wccontact", "china_link_contact",
                                    "friend_contact", "contact_v2"):
                        continue
                    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{tb}")')]
                    low = {c.lower(): c for c in cols}
                    u = low.get("username") or low.get("user_name")
                    nick = low.get("nickname") or low.get("nick_name")
                    remark = low.get("remark")
                    if not u:
                        continue
                    try:
                        rows = con.execute(
                            f'SELECT "{u}","{nick or u}","{remark or u}" FROM "{tb}"'
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    for uid, nk, rk in rows:
                        uid = (uid or "").strip()
                        if not uid or uid.startswith(("gh_", "wxid_biz")):
                            continue
                        contacts.setdefault(uid, Contact(
                            cid=uid,
                            name=(rk or "").strip() or (nk or "").strip() or uid,
                            kind="group" if uid.endswith("@chatroom") else "friend",
                            alias=(nk or "").strip() or ""))
            finally:
                con.close()
        return sorted(contacts.values(), key=lambda c: c.name)

    # ---------------- 消息 ----------------
    def fetch(self, contact: Contact, start: date, end: date) -> List[Message]:
        key_hex = self._key_hex or self._obtain_key()
        self._key_hex = key_hex
        root = self._stage_and_decrypt()
        msg_db = root / "message" / "message_0.db"
        if not msg_db.exists():
            msg_dbs = sorted(root.rglob("message_*.db"))
            if not msg_dbs:
                raise SourceError("未找到 message_*.db 解密文件")
            msg_db = msg_dbs[0]
        
        md5 = hashlib.md5(contact.cid.lower().encode()).hexdigest()
        table_name = f"Msg_{md5}"
        
        try:
            con = sqlite3.connect(f"file:{msg_db.as_posix()}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as e:
            raise SourceError(f"无法打开消息库: {e}")
        
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            actual = None
            for t in tables:
                if t.lower() == table_name.lower():
                    actual = t; break
            if not actual:
                return []  # 无消息
            
            start_ts = int(datetime(start.year, start.month, start.day).timestamp())
            end_ts = int(datetime(end.year, end.month, end.day).timestamp())
            
            acc = self._resolve_account()
            self_rowid = None
            # Name2Id.user_name 存的是微信发送人 id(如 wxid_xxx), 而账号目录名可能是
            # wxid_xxx_effe 这种带环境后缀。逐候选匹配,避免都查不到导致 is_self 全 False。
            import re as _re
            acct = acc.account
            candidates = [acct]
            # "<账号>_effe" → 去掉 "_xxx"/"-xxx" 后缀得 "<账号>"
            base_name = _re.split(r"[_\-][^_\-]+$", acct)[0]
            if base_name and base_name != acct:
                candidates.append(base_name)
            try:
                for name in candidates:
                    r = con.execute("SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 1",
                                    (name,)).fetchone()
                    if r:
                        self_rowid = r[0]
                        break
            except Exception:
                pass
            
            con.row_factory = sqlite3.Row
            sql = (
                f"SELECT m.local_type,m.create_time,m.message_content,"
                f"m.compress_content,m.real_sender_id,"
                f"n.user_name AS sender_username "
                f"FROM \"{actual}\" m "
                f"LEFT JOIN Name2Id n ON m.real_sender_id=n.rowid "
                f"WHERE m.create_time>=? AND m.create_time<? "
                f"ORDER BY m.sort_seq ASC, m.local_id ASC"
            )
            rows = con.execute(sql, (start_ts, end_ts)).fetchall()
            
            msgs = []
            for row in rows:
                rid = row["real_sender_id"]
                is_sent = bool(self_rowid and rid == self_rowid)
                ct = int(row["create_time"] or 0)
                if ct > 100000000000: ct //= 1000
                msg_text = self._decode_msg(row["compress_content"],
                                            row["message_content"],
                                            int(row["local_type"] or 0))
                msgs.append(Message(
                    ts=datetime.fromtimestamp(ct),
                    sender=row["sender_username"] or "",
                    text=msg_text,
                    is_self=is_sent,
                    msg_type=TYPE_MAP.get(int(row["local_type"] or 0), f"type{row['local_type']}"),
                ))
            return msgs
        finally:
            con.close()

    def _decode_msg(self, cc, mc, lt):
        raw = None
        # 微信 4.x: zstd 压缩数据常直接存在 message_content(mc) 字段,
        # compress_content(cc) 往往为空字符串。因此两个字段都要检查 ZSTD_MAGIC。
        zstd_src = None
        for field in (mc, cc):
            if isinstance(field, bytes) and len(field) >= 4 and field[:4] == ZSTD_MAGIC:
                zstd_src = field
                break
        if zstd_src is not None:
            if _HAVE_ZSTD:
                try: raw = _ZSTD_CTX.decompress(zstd_src)
                except Exception: pass
                else:
                    if isinstance(raw, bytes) and len(raw) >= 6:
                        # zstd 帧可能带可选头(如 pb 长度), 若解出的不可读再退化为原字节
                        try:
                            _ = raw.decode("utf-8")
                        except Exception:
                            pass
            else:
                return "[ZSTD压缩 需安装zstandard库]"
        if raw is None:
            raw = (mc or b"") if isinstance(mc, bytes) else str(mc or "").encode("utf-8")
        if isinstance(raw, bytes):
            try: raw = raw.decode("utf-8", errors="replace")
            except: raw = raw.decode("utf-8", errors="replace")
        text = raw.strip()
        if lt == 1: return text
        if text.startswith("<"):
            m = re.search(r"<title>(.*?)</title>", text, re.S)
            if m: return f"[{TYPE_MAP.get(lt, f'类型{lt}')}] {m.group(1).strip()}"
            plain = re.sub(r"<[^>]+>", "", text).strip()
            if plain: return f"[{TYPE_MAP.get(lt, f'类型{lt}')}] {plain}"
            return f"[{TYPE_MAP.get(lt, f'类型{lt}')}]"
        return f"[{TYPE_MAP.get(lt, f'类型{lt}')}] {text[:200]}" if text else f"[{TYPE_MAP.get(lt, f'类型{lt}')}]"

    


def _is_decrypted(path: Path) -> bool:
    try:
        return path.read_bytes()[:16] == b"SQLite format 3\x00"
    except Exception:
        return False
