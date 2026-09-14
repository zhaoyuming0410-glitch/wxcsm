"""我方成员名称库 —— 把同事名字长期存起来，不用每次重输。

两个概念必须分清，混在一起会出问题：

* **名称库**（`self_name_library`）= 你认过的全部我方人员，长期存着，不参与本次运行。
* **本次选用**（`self_names`）= 这一次跑要把哪些人算作「我方」。

如果库直接等于选用名单，「取消勾选某人」就变成「把他从库里删掉」，
下次又得重新导入——所以库只增不减（要删得显式点删除）。

名字有三个来源：手动输入、Excel 导入、从聊天记录扫出的候选。三条都要先过
`clean_names()`：员工花名册这类表格里混着「序号」「部门」「手机号」「员工姓名」
表头，原样存进配置的话，轻则列表难看，重则把某个部门名当成同事——
`wx4_live` 是用**子串匹配**判我方身份的，一个短词误入库会误伤真实客户发言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# 超过这个长度的一律不是姓名（多半是整句备注或地址）
MAX_NAME_CHARS = 20
# 单个 sheet 最多读这么多行，够覆盖员工名单，又不至于把大表读爆
MAX_ROWS = 2000

# 表头/字段名：中文姓名列常写作「姓名」「员工姓名」「客户名称」，
# 所以既匹配整串等于，也匹配以这些词结尾。
_HEADER_WORD = re.compile(
    r"^(姓名|名字|名称|名单|成员|人员|负责人|经办人|花名|昵称|备注|"
    r"部门|职位|岗位|手机|手机号|电话|电话号码|联系方式|身份证|邮箱|邮箱地址|"
    r"工号|序号|编号|微信|微信名|备注名|用户名|账号|帐号|账户|"
    r"name|fullname|username)$"
    r"|(姓名|名字|名单|成员|人员|名称)$", re.I)
# 明确不是姓名但容易混进来的：数字（序号/工号/手机号，含 Excel 的浮点单元格）、
# 日期、邮箱、多行文本
_NOT_NAME = re.compile(
    r"^\d+(\.\d+)?$"                      # 12 / 12.0 / 12.5 这类 Excel 数字单元格
    r"|^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"   # 日期
    r"|^\d{6,}$"                          # 工号/手机号
    r"|[@/\\]"                            # 邮箱、路径
    r"|\n"                                # 多行
)
# 部门/组织名，不是人。单字后缀（部/科/组/室/处）要求整串至少 3 字，
# 否则「张科」这种真名会被误杀——名称库漏一个真名只是少个人，
# 但误杀会让人莫名其妙地发现同事的答复没被算作我方。
_NOT_PERSON = re.compile(r"(中心|公司|集团|事业部|办事处|团队|小组|委员会)$"
                         r"|.{2,}(部|科|组|室|处)$")
# 名字里至少有「文字」：中日韩字符或拉丁字母。纯符号/数字不算。
_HAS_TEXT = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af0-9A-Za-z]")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 判断「像不像姓名列」的表头关键词，以及不应出现在姓名列里的词
_LIKELY_NAME_HEADER = re.compile(r"姓名|名字|名称|成员|人员|name", re.I)

# 表头常见的装饰尾巴：「姓名*」「姓名（必填）」「员工姓名[必填]」。
# 不剥掉的话 _HEADER_WORD 匹配不上（它要求整串等于或以「姓名」结尾），
# 结果**表头本身会被当成一个人名导进名称库**——真实模板几乎都这么写，所以必须处理。
_HEADER_DECOR_TAIL = re.compile(
    r"[\s*＊]+$"                                   # 尾部的空格和星号
    r"|[（(\[【][^）)\]】]{0,10}[）)\]】]\s*$"        # 尾部的括号标注
)


def undecorate_header(s: str) -> str:
    """剥掉表头装饰，便于识别「姓名*」「姓名（必填）」这类写法。

    只用于比对，不改变原始值；「李四（销售）」→「李四」，比对不受影响。
    """
    prev = None
    while prev != s:
        prev = s
        s = _HEADER_DECOR_TAIL.sub("", s).strip()
    return s


def clean_names(raw: Iterable[Any]) -> List[str]:
    """把一列原始单元格值清洗成姓名，去空、去重、保序。

    宁可漏掉几个可疑值，也不要把「研发部」「13800138000」存进名称库：
    库里一个短词被当成同事，会让真实客户的发言被误判为我方。
    """
    out: List[str] = []
    seen: set = set()
    for v in raw:
        if v is None:
            continue
        s = str(v).replace("\u3000", " ").strip()
        s = re.sub(r"\s+", " ", s)
        if not s or len(s) > MAX_NAME_CHARS:
            continue
        if not _HAS_TEXT.search(s):
            continue
        if _HEADER_WORD.search(s) or _HEADER_WORD.search(undecorate_header(s)):
            continue
        if _NOT_NAME.search(s) or _NOT_PERSON.search(s):
            continue
        # 姓名几乎不会很长；超过 10 个字的多半是备注/公司名
        if len(s) > 10 and not _CJK.search(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def looks_like_person(name: str) -> bool:
    """这个词像不像人名。

    只用于**提示**，不用来自动过滤手动输入的名字：用户明确敲进来的名字应当尊重，
    静默丢掉会让人以为"加了但没生效"。而 Excel 导入是批量读取，用 clean_names
    直接过滤——那种场景里混进表头/部门/手机号是常态。
    """
    s = (name or "").strip()
    if not s or len(s) > MAX_NAME_CHARS:
        return False
    if (_HEADER_WORD.search(s) or _HEADER_WORD.search(undecorate_header(s))
            or _NOT_NAME.search(s) or _NOT_PERSON.search(s)):
        return False
    return bool(_HAS_TEXT.search(s))


def suspect_names(names: Iterable[str]) -> List[str]:
    """挑出「看起来不像人名」的，供界面/命令行提醒用户。"""
    return [n for n in names or [] if not looks_like_person(n)]


def merge_names(*groups: Iterable[str]) -> List[str]:
    """按顺序合并多组名字并去重（先出现的优先）。"""
    out: List[str] = []
    seen: set = set()
    for g in groups:
        for n in g or []:
            n = str(n).strip()
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def split_names(text: str) -> List[str]:
    """把界面上「张三, 李四」这类输入切成名字列表（支持中文逗号/顿号/换行/分号）。"""
    return [x.strip() for x in re.split(r"[,，、;；\n\r]+", text or "") if x.strip()]


@dataclass
class NameColumn:
    """Excel 里的一列，作为「候选姓名列」。"""

    sheet: str
    index: int                       # 0 基列号
    header: str                      # 首行文字（无表头时留空）
    names: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """给下拉框显示用，如「Sheet1 · 第2列（员工姓名）」。"""
        col = f"第{self.index + 1}列"
        return f"{self.sheet} · {col}（{self.header}）" if self.header else f"{self.sheet} · {col}"

    @property
    def likely_name_column(self) -> bool:
        """表头像不像姓名列。"""
        return bool(_LIKELY_NAME_HEADER.search(self.header or ""))

    @property
    def has_any_name(self) -> bool:
        # 表头本身也会被 clean_names 过滤掉，所以这里看清洗**之前**有没有内容
        return bool(self.header) or bool(self.names)


def read_columns(path: str | Path, max_rows: int = MAX_ROWS) -> List[NameColumn]:
    """读 Excel 的所有 sheet 的所有列，返回可选的姓名列。

    不预先假定「姓名一定在第一列」：真实员工表常把序号/部门放前面，
    所以把每一列都列出来交给用户选，并给出猜测。
    """
    from openpyxl import load_workbook

    cols: List[NameColumn] = []
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = list(islice(ws.iter_rows(values_only=True), max_rows))
            if not rows:
                continue
            width = max((len(r) for r in rows), default=0)
            for ci in range(width):
                vals = [r[ci] if ci < len(r) else None for r in rows]
                if all(v in (None, "") for v in vals):
                    continue
                header = ""
                for v in vals:
                    if v not in (None, ""):
                        header = re.sub(r"\s+", " ", str(v)).strip()
                        break
                cols.append(NameColumn(sheet=ws.title or "Sheet1", index=ci,
                                       header=header, names=clean_names(vals)))
    finally:
        wb.close()
    return cols


def guess_column(cols: Sequence[NameColumn]) -> Optional[NameColumn]:
    """猜哪一列是姓名列：先看表头，再看哪列的"名字"最多。

    猜错不要紧——GUI 里用户能自己改选；这里只是让默认选择大概率正确。
    """
    usable = [c for c in cols if c.names]
    if not usable:
        return None
    headed = [c for c in usable if c.likely_name_column]
    if headed:
        # 有多个像姓名列时，取名字最多的那个
        return max(headed, key=lambda c: len(c.names))
    return max(usable, key=lambda c: len(c.names))


# ---------------- 导入模版 ----------------

TEMPLATE_SHEET = "我方成员"
TEMPLATE_FILENAME = "我方成员名称库模版.xlsx"
# (表头, 是否必填)。必填的表头在导出的模版里标红色。
TEMPLATE_COLUMNS = (("姓名", True), ("部门", False), ("手机号", False), ("备注", False))
TEMPLATE_REQUIRED_COLOR = "FFC00000"
TEMPLATE_HINT = (
    "填写说明：\n"
    "1) 只填「姓名」列，一人一行，最少填一个；姓名列已带出你当前的名称库。\n"
    "2) 部门 / 手机号 / 备注随便填，导入时会被自动忽略（名称库只存姓名）。\n"
    "3) 要加人就在下面接着写，要删人就把那一行整行删掉。\n"
    "4) 填好后回到「选择我方成员」窗口，点「导入 Excel…」选这个文件。\n"
    "5) 导入会**按这个文件更新名称库**：文件里没有的人会从库里移除"
    "（导入前会列出将要新增和删除的名单，确认后才生效）。"
)


def write_template(path: str | Path, names: Iterable[str] = ()) -> Path:
    """导出一份导入模版：表头（必填项标红）+ 把当前名称库的人填进「姓名」列。

    填进去的是**用户自己的名称库**，不是示例数据——于是「导出 → 在 Excel 里
    增删 → 导入」构成一个闭环：导入时以这个文件为准更新名称库（见 app.py 里
    导入前的确认弹窗）。

    为什么坚决不造假的示例名字：用户多半会直接填在示例下面，示例名字就被一起
    导进名称库了；而名称库是靠**子串匹配**判我方身份的，混进一个泛词就可能误伤
    真实客户发言。用户自己的库成员没有这个问题——他们本来就在库里。

    `names` **刻意不过滤**（不调 clean_names）：手动添加的名字按设计就不过滤
    ——"你明确敲进来的名字应当尊重"。导出时再过滤一遍，等于把用户刚加的人
    偷偷从模版里抹掉，他照着改完导入回来就发现人没了。
    """
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    p = Path(path)
    wb = Workbook()
    ws = wb.active
    ws.title = TEMPLATE_SHEET
    for ci, (name, required) in enumerate(TEMPLATE_COLUMNS, start=1):
        cell = ws.cell(row=1, column=ci)
        # 表头写成「姓名（必填）」也能被 clean_names 正确识别为表头，
        # 因为它会先剥掉括号标注再比对（见 undecorate_header）。
        cell.value = f"{name}（必填）" if required else f"{name}（可选）"
        if required:
            cell.font = Font(color=TEMPLATE_REQUIRED_COLOR, bold=True)
        else:
            cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(ci)].width = (16 if required else 14)
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"
    ws["A1"].comment = Comment(TEMPLATE_HINT, "wxcsm")
    # 名称库里的人填在「姓名」列（第一列）。只写名字，部门/手机号/备注留空：
    # 名称库本来就只存名字，那三列是给用户自己看着方便的。
    for r, n in enumerate(names, start=2):
        ws.cell(row=r, column=1, value=str(n))
    wb.save(str(p))
    return p
