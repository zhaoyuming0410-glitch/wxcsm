# -*- coding: utf-8 -*-
"""命令行入口 —— 供 WorkBuddy 自动化工作流调用。

与 GUI 共用 core/ 下的同一套引擎，因此两边行为完全一致。
所有子命令都支持 --json，输出机器可读结果，便于上游编排。

用法示例：
  python cli.py list-sources
  python cli.py list-contacts --source demo --json
  python cli.py list-contacts --source import --path D:/导出记录 --search 恒瑞
  python cli.py run --source demo --all --preset 30d --out D:/总结.xlsx --json
  python cli.py run --source import --path D:/导出记录 --contacts "恒瑞,蔚蓝" \
                    --start 2026-08-01 --end 2026-08-31 --engine offline
  python cli.py gui
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core import config, pipeline
from core.models import Contact
from core.sources import ORDER, REGISTRY, SourceError, choices
from core.wx4 import detect_wechat_env, keyring, memkey
from core.wx4.errors import KeyNotFoundError, WechatNotRunningError


def _emit(payload: Dict, as_json: bool, lines: List[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n".join(lines))


def _cfg_from_args(args) -> Dict:
    cfg = config.load()
    if getattr(args, "source", None):
        cfg["source"] = args.source
    if getattr(args, "path", None):
        cfg["source_path"] = args.path
    if getattr(args, "self_names", None):
        cfg["self_names"] = [x.strip() for x in args.self_names.replace("，", ",").split(",")
                              if x.strip()]
    if getattr(args, "engine", None):
        cfg["engine"] = args.engine
    if getattr(args, "out", None):
        cfg["export_path"] = args.out
    if getattr(args, "mode", None):
        cfg["export_mode"] = args.mode
    if getattr(args, "min_chars", None):
        cfg["min_chars"] = args.min_chars
    if getattr(args, "max_chars", None):
        cfg["max_chars"] = args.max_chars
    if getattr(args, "api_key", None):
        cfg["llm_api_key"] = args.api_key
    if getattr(args, "model", None):
        cfg["llm_model"] = args.model
    if getattr(args, "base_url", None):
        cfg["llm_base_url"] = args.base_url
    return cfg


# ---------------- 子命令 ----------------
def cmd_list_sources(args) -> int:
    data = [{"key": k, "label": lb, "hint": h, "needs_path": np,
             "path_label": pl, "path_kind": REGISTRY[k].path_kind}
            for k, lb, h, np, pl in choices()]
    lines = ["可用数据源："]
    for d in data:
        lines.append(f"  {d['key']:8s} {d['label']}")
        lines.append(f"           {d['hint']}")
        if d["needs_path"]:
            lines.append(f"           需要 --path 指定{d['path_label']}（{d['path_kind']}）")
    _emit({"sources": data}, args.json, lines)
    return 0


def cmd_list_contacts(args) -> int:
    cfg = _cfg_from_args(args)
    try:
        contacts = pipeline.load_contacts(cfg)
    except SourceError as e:
        _emit({"ok": False, "error": str(e)}, args.json, [f"[失败] {e}"])
        return 2
    if args.search:
        contacts = pipeline.filter_contacts(contacts, args.search)
    data = [{"cid": c.cid, "name": c.name, "kind": c.kind, "alias": c.alias,
             "msg_count": c.msg_count,
             "last_time": c.last_time.isoformat() if c.last_time else None}
            for c in contacts]
    lines = [f"共 {len(data)} 个聊天对象（数据源：{cfg['source']}）"]
    lines += [f"  {c.display}" for c in contacts]
    _emit({"ok": True, "count": len(data), "contacts": data}, args.json, lines)
    return 0


def _resolve(contacts: List[Contact], args) -> tuple[List[Contact], List[str]]:
    """把 --contacts / --search / --all 解析成具体对象列表。"""
    if args.all:
        return list(contacts), []
    picked: List[Contact] = []
    missing: List[str] = []
    if args.search:
        picked = pipeline.filter_contacts(contacts, args.search)
    if args.contacts:
        by_key = {c.cid: c for c in contacts}
        for raw in args.contacts.split(","):
            token = raw.strip()
            if not token:
                continue
            if token in by_key:
                hit = [by_key[token]]
            else:
                hit = [c for c in contacts if token.lower() in
                       f"{c.name} {c.alias} {c.cid}".lower()]
            if not hit:
                missing.append(token)
                continue
            for c in hit:
                if c not in picked:
                    picked.append(c)
    return picked, missing


def cmd_run(args) -> int:
    cfg = _cfg_from_args(args)
    try:
        if args.preset:
            start, end = pipeline.preset_range(args.preset)
        else:
            if not (args.start and args.end):
                raise ValueError("请提供 --start 与 --end，或使用 --preset。")
            start = pipeline.parse_day(args.start, "开始日期")
            end = pipeline.parse_day(args.end, "结束日期")
    except ValueError as e:
        _emit({"ok": False, "error": str(e)}, args.json, [f"[失败] {e}"])
        return 2

    try:
        contacts = pipeline.load_contacts(cfg)
    except SourceError as e:
        _emit({"ok": False, "error": str(e)}, args.json, [f"[失败] {e}"])
        return 2

    picked, missing = _resolve(contacts, args)
    if not picked:
        msg = "没有匹配到任何聊天对象。请用 list-contacts 先确认名称，或加 --all。"
        _emit({"ok": False, "error": msg, "missing": missing}, args.json, [f"[失败] {msg}"])
        return 2

    def prog(i: int, t: int, text: str) -> None:
        if not args.json and not args.quiet:
            print(f"[{i}/{t}] {text}", flush=True)

    try:
        res = pipeline.run(cfg, picked, start, end, progress=prog,
                           do_export=not args.no_export)
    except Exception as e:
        _emit({"ok": False, "error": str(e)}, args.json, [f"[失败] {e}"])
        return 1

    data = {
        "ok": True,
        "source": cfg["source"],
        "engine": cfg["engine"],
        "time_range": f"{start.isoformat()} ~ {end.isoformat()}",
        "requested": len(picked),
        "generated": res.ok_count,
        "skipped": res.skipped,
        "unmatched": missing,
        "export_path": res.export_path,
        "written_rows": res.written,
        "total_rows": res.total_rows,
        "summaries": [{"客户名称": s.customer_name, "时间范围": s.time_range,
                       "总结内容": s.content, "字数": s.char_len,
                       "引擎": s.engine, "备注": s.error}
                      for s in res.summaries],
    }
    lines = [
        "",
        f"时间范围：{data['time_range']}",
        f"生成总结：{res.ok_count} / {len(picked)} 个对象",
    ]
    for s in res.summaries:
        lines.append(f"\n【{s.customer_name}】{s.time_range}  ({s.char_len} 字 / {s.engine})")
        lines.append(f"  {s.content}")
        if s.error:
            lines.append(f"  [备注] {s.error}")
    if res.skipped:
        lines.append("\n被跳过：")
        lines += [f"  · {x}" for x in res.skipped]
    if missing:
        lines.append(f"\n未匹配到的名称：{', '.join(missing)}")
    if res.export_path:
        lines.append(f"\nExcel：{res.export_path}（本次写入 {res.written} 行，"
                     f"共 {res.total_rows} 行数据）")
    _emit(data, args.json, lines)
    return 0


def cmd_gui(args) -> int:
    import app
    app.main()
    return 0


def cmd_wx4_capture(args) -> int:
    """抓取微信 4.x 数据库密钥(只读内存扫描)。"""
    from core.wx4.locate import detect_wechat_env
    env = detect_wechat_env()
    acc = env.primary_account
    if not acc:
        _emit({"ok": False, "error": "未找到微信 4.x 账号数据目录"},
              args.json, ["[失败] 未找到微信 4.x 账号数据目录"])
        return 2
    cached = keyring.load_key(acc.account)
    if cached and not args.force:
        _emit({"ok": True, "note": "密钥已缓存, 跳过抓取", "account": acc.account},
              args.json, [f"账号 {acc.account} 的密钥已缓存, 跳过抓取"])
        return 0
    try:
        key = memkey.capture_key(acc, progress_cb=lambda s: print(s, flush=True),
                                 restart=args.restart, timeout=args.timeout)
        keyring.save_key(acc.account, key,
                         wechat_version=env.version or "",
                         wechat_exe=env.exe_path or "")
        _emit({"ok": True, "account": acc.account, "key": key},
              args.json, [f"✓ 成功抓取账号 {acc.account} 的密钥"])
        return 0
    except (KeyNotFoundError, WechatNotRunningError, Exception) as e:
        _emit({"ok": False, "error": str(e)},
              args.json, [f"[失败] {e}"])
        return 2


# ---------------- 参数解析 ----------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wxcsm",
        description="微信客户沟通记录提取与总结工具（命令行入口，供 WorkBuddy 调用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, need_range: bool = False):
        sp.add_argument("--source", choices=ORDER,
                        help="数据源：demo / import / wxdb / wx4（本机微信 4.x 加密库）")
        sp.add_argument("--path", help="数据路径（import / wxdb 填文件夹或 .zip 压缩包）")
        sp.add_argument("--json", action="store_true", help="以 JSON 输出")

    sp = sub.add_parser("list-sources", help="列出可用数据源")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list_sources)

    sp = sub.add_parser("list-contacts", help="列出可选聊天对象")
    add_common(sp)
    sp.add_argument("--search", help="按关键词过滤")
    sp.set_defaults(func=cmd_list_contacts)

    sp = sub.add_parser("run", help="提取聊天记录、生成总结并导出 Excel")
    add_common(sp)
    sp.add_argument("--self-names", dest="self_names",
                   help="代表「我方」的发送人名，逗号分隔（如 李娜,分贝通客服）。"
                        "导入源（真实姓名无「我方」标记时还原对接进度）；解密数据库源（校正群聊偶尔归错的人、统一显示你的名字）")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--contacts", help="聊天对象名称，逗号分隔，支持部分匹配")
    g.add_argument("--all", action="store_true", help="处理全部聊天对象")
    sp.add_argument("--search", help="按关键词批量选择对象")
    sp.add_argument("--preset", choices=[k for k, _ in pipeline.PRESETS],
                    help="时间范围快捷方式")
    sp.add_argument("--start", help="开始日期 2026-08-01")
    sp.add_argument("--end", help="结束日期 2026-08-31")
    sp.add_argument("--engine", choices=["auto", "ai", "offline"], help="总结引擎")
    sp.add_argument("--out", help="Excel 输出路径")
    sp.add_argument("--mode", choices=["append", "overwrite"], help="写入方式")
    sp.add_argument("--min-chars", type=int, dest="min_chars", help="总结最少字数")
    sp.add_argument("--max-chars", type=int, dest="max_chars", help="总结最多字数")
    sp.add_argument("--api-key", dest="api_key", help="AI 密钥（也可用环境变量注入）")
    sp.add_argument("--model", help="AI 模型名")
    sp.add_argument("--base-url", dest="base_url", help="AI 接口地址")
    sp.add_argument("--no-export", action="store_true", help="只生成不写 Excel")
    sp.add_argument("--quiet", action="store_true", help="不打印进度")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("gui", help="启动图形界面")
    sp.set_defaults(func=cmd_gui, json=False)

    sp = sub.add_parser("wx4-capture", help="抓取微信 4.x 数据库密钥（只读内存扫描）")
    sp.add_argument("--force", action="store_true", help="强制重新抓取（覆盖缓存）")
    sp.add_argument("--restart", action="store_true",
                    help="自动重启微信后抓取（需要等待登录）")
    sp.add_argument("--timeout", type=int, default=240,
                    help="重启模式下的最大等待秒数（默认 240）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_wx4_capture)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
