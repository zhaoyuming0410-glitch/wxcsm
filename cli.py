# -*- coding: utf-8 -*-
"""命令行入口 —— 供 WorkBuddy 自动化工作流调用。

与 GUI 共用 core/ 下的同一套引擎，因此两边行为完全一致。
所有子命令都支持 --json，输出机器可读结果，便于上游编排。

用法示例：
  python cli.py list-sources
  python cli.py list-contacts --source demo --json
  python cli.py list-contacts --source import --path D:/导出记录 --search 甲乙
  python cli.py run --source demo --all --preset 30d --out D:/总结.xlsx --json
  python cli.py run --source import --path D:/导出记录 --contacts "甲乙,丙丁" \
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

from core import config, name_library, pipeline
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
    if getattr(args, "output_format", None):
        cfg["output_format"] = args.output_format
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
        "output_format": cfg.get("output_format", "both"),
        "time_range": f"{start.isoformat()} ~ {end.isoformat()}",
        "requested": len(picked),
        "generated": res.ok_count,
        "qa_count": res.qa_count,
        "qa_engine": res.qa_engine,
        "qa_notes": res.qa_notes,
        "skipped": res.skipped,
        "unmatched": missing,
        "export_path": res.export_path,
        "written_rows": res.written,
        "total_rows": res.total_rows,
        "qa_export_path": res.qa_export_path,
        "qa_written_rows": res.qa_written,
        "qa_total_rows": res.qa_total_rows,
        "summaries": [{"客户名称": s.customer_name, "时间范围": s.time_range,
                       "总结内容": s.content, "字数": s.char_len,
                       "引擎": s.engine, "备注": s.error}
                      for s in res.summaries],
        "qa_items": [{"客户名称": it.customer_name, "类型": it.kind,
                      "提问时间": it.ask_time, "客户问题": it.question_cell,
                      "我方答复": it.answer, "答复时间": it.answer_time,
                      "状态": it.status, "原始记录": it.raw}
                     for it in res.qa_items],
    }
    lines = [
        "",
        f"时间范围：{data['time_range']}",
        f"产出形式：{data['output_format']}",
    ]
    if res.summaries:
        lines.append(f"生成总结：{res.ok_count} / {len(picked)} 个对象")
        for s in res.summaries:
            lines.append(f"\n【{s.customer_name}】{s.time_range}  ({s.char_len} 字 / {s.engine})")
            lines.append(f"  {s.content}")
            if s.error:
                lines.append(f"  [备注] {s.error}")
    if res.qa_items or res.qa_engine:
        eng = {"ai": "AI", "offline": "离线规则", "ai+offline": "AI + 离线（部分降级）"}.get(
            res.qa_engine, res.qa_engine or "离线规则")
        lines.append(f"\n客户问题答复清单：{res.qa_count} 条（{res.qa_kinds}）［引擎：{eng}］")
        # 降级原因必须说出来：用户选了 AI 却拿到离线结果，不说就是欺骗
        for note in res.qa_notes:
            lines.append(f"  [注意] {note}")
        cur = None
        for it in res.qa_items:
            if it.customer_name != cur:
                cur = it.customer_name
                lines.append(f"\n【{cur}】")
            lines.append(f"  [{it.kind}] [{it.status}] {it.ask_time}")
            lines.append(f"     问：{it.question_cell}")
            lines.append(f"     答：{it.answer or '（无）'}")
    if res.skipped:
        lines.append("\n被跳过：")
        lines += [f"  · {x}" for x in res.skipped]
    if missing:
        lines.append(f"\n未匹配到的名称：{', '.join(missing)}")
    if res.export_path:
        lines.append(f"\nExcel 沟通总结：{res.export_path}"
                     f"（本次写入 {res.written} 行，表内共 {res.total_rows} 行）")
    if res.qa_export_path:
        lines.append(f"Excel 问题答复清单：{res.qa_export_path}"
                     f"（本次写入 {res.qa_written} 条，表内共 {res.qa_total_rows} 条）")
    _emit(data, args.json, lines)
    return 0


def cmd_suggest_self(args) -> int:
    """从一段聊天记录里找出「可能是我方同事」的人，供用户确认后填 --self-names。

    只给候选和理由，不自动改配置——判定"谁是同事"是业务问题，
    猜错会把客户的话当成我方答复，比漏掉更难发现。
    """
    from core.self_discover import suggest_self_members

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
        print(f"[失败] {e}")
        return 2

    try:
        contacts = pipeline.load_contacts(cfg)
    except SourceError as e:
        print(f"[失败] {e}")
        return 2

    picked, missing = _resolve(contacts, args)
    if not picked:
        print("没有匹配到聊天对象。请用 list-contacts 先确认名称。")
        return 1

    data_items = []
    # 名称库（长期存）与本次选用（self_names）都算「已知的我方成员」：
    # 库里认过的同事没必要再作为候选推一遍。
    known = name_library.merge_names(cfg.get("self_name_library") or [],
                                     cfg.get("self_names") or [])
    lines = [f"时间范围：{start:%Y-%m-%d} ~ {end:%Y-%m-%d}",
             f"已知我方成员：{', '.join(known) or '（未设置）'}", ""]
    src = pipeline.build_source(cfg)
    for contact in picked:
        try:
            msgs = src.fetch(contact, start, end)
        except Exception as e:  # noqa: BLE001
            lines.append(f"【{contact.name}】提取失败：{e}")
            continue
        cands = suggest_self_members(
            msgs, known_self=known, owner_names=known,
            top=int(getattr(args, "top", 15) or 15))
        lines.append(f"【{contact.name}】{len(msgs)} 条消息，候选 {len(cands)} 人：")
        if not cands:
            lines.append("  （没有明显像我方同事的成员）")
        for i, c in enumerate(cands, start=1):
            lines.append(f"  {i}. {c.describe()}")
        picks = [c.suggestion for c in cands[:3] if c.score >= 40]
        if picks:
            lines.append(f"  → 建议先试：--self-names \"{','.join(picks)}\"")
        lines.append("")
        data_items.append({"对象": contact.name, "消息数": len(msgs),
                           "候选": [{"名字": c.name, "账号": c.cid,
                                     "发言数": c.msg_count, "评分": c.score,
                                     "依据": c.reasons} for c in cands]})
    if missing:
        lines.append(f"未匹配到的名称：{', '.join(missing)}")
    lines.append("提示：确认哪些是同事后，把它填进配置的「我方成员」(self_names)，"
                 "问答清单的答复配对才会准。")
    _emit({"time_range": f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}",
           "known_self": cfg.get("self_names") or [], "results": data_items},
          args.json, lines)
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
        description="绿泡泡聊天记录总结（命令行入口，供 WorkBuddy 调用）",
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
                   help="代表「我方」的发送人名，逗号分隔（如 李四,壬癸客服,张三）。"
                        "群聊里「我方」往往不止账号本人——同事用自己的号或企微/OpenIM 桥接号发言时，"
                        "不填会被当成客户发言，问答清单会大面积显示「未答复」。支持部分匹配，"
                        "填「张三」可匹配到备注「张三 交付」")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--contacts", help="聊天对象名称，逗号分隔，支持部分匹配")
    g.add_argument("--all", action="store_true", help="处理全部聊天对象")
    sp.add_argument("--search", help="按关键词批量选择对象")
    sp.add_argument("--preset", choices=[k for k, _ in pipeline.PRESETS],
                    help="时间范围快捷方式")
    sp.add_argument("--start", help="开始日期 2026-08-01")
    sp.add_argument("--end", help="结束日期 2026-08-31")
    sp.add_argument("--engine", choices=["auto", "ai", "offline"], help="总结引擎")
    sp.add_argument("--output-format", dest="output_format",
                    choices=[k for k, _ in pipeline.OUTPUT_FORMATS],
                    help="产出形式：both=沟通总结+客户问题答复清单（默认）；"
                         "summary=仅沟通总结；qa=仅客户问题答复清单")
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
                    help="[保留参数，当前无实际作用] 抓取失败时请**手动**退出微信再重新打开"
                         "登录，然后重试；本工具不会替你关闭微信")
    sp.add_argument("--timeout", type=int, default=240,
                    help="抓取的最大等待秒数（默认 240）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_wx4_capture)

    sp = sub.add_parser("suggest-self",
                        help="从一段群聊里找出「可能是我方同事」的人（用于填 --self-names）")
    sp.add_argument("--source", required=True, help="数据源 key（见 list-sources）")
    sp.add_argument("--path", help="导入源/已解密库的文件或目录")
    sp.add_argument("--contacts", help="要分析的聊天对象名，逗号分隔")
    sp.add_argument("--all", action="store_true", help="分析全部聊天对象")
    sp.add_argument("--search", help="按关键字模糊匹配聊天对象名")
    sp.add_argument("--preset", choices=[k for k, _ in pipeline.PRESETS], default="30d")
    sp.add_argument("--start", help="开始日期，如 2026-08-01")
    sp.add_argument("--end", help="结束日期，如 2026-08-31")
    sp.add_argument("--self-names", dest="self_names",
                    help="已知的我方成员，逗号分隔（这些人不再作为候选）")
    sp.add_argument("--top", type=int, default=15, help="最多列出几个候选（默认 15）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_suggest_self)

    sp = sub.add_parser(
        "config", help="查看/修改我方成员名称库与常用配置")
    sp.add_argument("--show", action="store_true", help="显示当前名称库与选用名单（默认动作）")
    sp.add_argument("--add-library", dest="add_library",
                    help="往名称库里追加姓名，逗号分隔")
    sp.add_argument("--remove-library", dest="remove_library",
                    help="从名称库里删除姓名，逗号分隔")
    sp.add_argument("--self-names", dest="self_names",
                    help="设置本次选用的我方成员（逗号分隔）；清空传空串")
    sp.add_argument("--import-library", dest="import_library", metavar="FILE",
                    help="从 Excel 更新名称库（.xlsx）——与界面一致：**以文件为准**，"
                         "文件里没有的人会从库里移除；会删人时必须加 --force")
    sp.add_argument("--export-template", dest="export_template", nargs="?", metavar="PATH",
                    const=name_library.TEMPLATE_FILENAME,
                    help="把当前名称库导出成 Excel 模版（同界面「导出 Excel 模版」："
                         "表头必填标红 + 把库里的人填在姓名列，改完可用 --import-library "
                         "导回）；省略路径则写到当前目录的 "
                         + name_library.TEMPLATE_FILENAME)
    sp.add_argument("--force", action="store_true",
                    help="--import-library 将要删人时确认执行（等价于界面上点「确认」）")
    sp.add_argument("--sheet", help="导入时指定 sheet 名（默认：自动猜列）")
    sp.add_argument("--column", type=int, help="导入时指定列号，从 1 开始（默认：自动猜列）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_config)
    return p


def cmd_config(args) -> int:
    """查看/修改我方成员名称库与选用名单。

    名称库（self_name_library）是长期存的名字池，本次选用（self_names）是这次跑
    要用的人。库里留着不代表这次要用，所以两个键分开操作。
    """
    cfg = config.load()
    lib = list(cfg.get("self_name_library") or [])
    sel = list(cfg.get("self_names") or [])
    notes: List[str] = []

    if args.add_library and args.import_library:
        print("--add-library 与 --import-library 不能一起用：导入是「以文件为准」的"
              "替换，会把刚加上的人冲掉。要加人用 --add-library，"
              "要按文件覆盖用 --import-library。", file=sys.stderr)
        return 2

    if args.add_library:
        before = len(lib)
        added = name_library.split_names(args.add_library)
        lib = name_library.merge_names(lib, added)
        notes.append(f"名称库新增 {len(lib) - before} 人")
        odd = name_library.suspect_names(added)
        if odd:
            notes.append(f"⚠ 这些看起来不像人名，确认一下：{', '.join(odd)}"
                         "（名称库用部分匹配判我方身份，泛词可能误伤客户发言）")

    if args.import_library:
        try:
            cols = name_library.read_columns(args.import_library)
        except Exception as e:  # noqa: BLE001
            print(f"读取失败：{e}", file=sys.stderr)
            return 1
        if args.column or args.sheet:
            cols = [c for c in cols
                    if (not args.sheet or c.sheet == args.sheet)
                    and (not args.column or c.index == args.column - 1)]
        col = None
        if args.column:
            col = cols[0] if cols else None
        else:
            col = name_library.guess_column(cols)
        if col is None or not col.names:
            print("没有找到可导入的姓名列。可用 --sheet/--column 指定，"
                  "或先用 GUI 的「导入 Excel…」核对。", file=sys.stderr)
            return 1
        incoming = list(col.names)
        keep = set(incoming)
        removed = [n for n in lib if n not in keep]
        added = [n for n in incoming if n not in set(lib)]
        # 会删人就要 --force。界面上这里弹确认框；命令行跑批没人看着，
        # 所以默认直接拒绝，而不是静默把库清掉。
        if removed and not args.force:
            print(f"按「{col.label}」更新名称库会移除 {len(removed)} 人："
                  f"{', '.join(removed)}", file=sys.stderr)
            if added:
                print(f"（同时新增 {len(added)} 人：{', '.join(added)}）", file=sys.stderr)
            print("确认无误就加 --force 重跑。", file=sys.stderr)
            return 1
        lib = incoming
        # 与 --remove-library 同理：库里已经没了却还留在「本次选用」里是矛盾状态
        sel = [n for n in sel if n in keep]
        notes.append(f"按「{col.label}」更新名称库：新增 {len(added)} 人、"
                     f"移除 {len(removed)} 人，现共 {len(lib)} 人")

    if args.remove_library:
        drop = set(name_library.split_names(args.remove_library))
        gone = [n for n in lib if n in drop]
        lib = [n for n in lib if n not in drop]
        # 删除也要影响本次选用，否则会出现"库里没了但还在用"的矛盾状态
        sel = [n for n in sel if n not in drop]
        notes.append(f"名称库删除 {len(gone)} 人" +
                     (f"（{', '.join(gone)}）" if gone else "（本来就没有）"))

    if args.self_names is not None:
        sel = name_library.split_names(args.self_names)
        notes.append(f"本次选用设为 {len(sel)} 人")
        odd = name_library.suspect_names(sel)
        if odd:
            notes.append(f"⚠ 这些看起来不像人名，确认一下：{', '.join(odd)}")

    # 导出放在最后：让它反映上面刚做完的新增/移除，导出的就是"改完的库"
    if args.export_template:
        try:
            out = name_library.write_template(args.export_template, lib)
        except Exception as e:  # noqa: BLE001
            print(f"导出模版失败：{e}", file=sys.stderr)
            return 1
        notes.append(f"模版已导出：{out}（带出名称库 {len(lib)} 人）")

    cfg["self_name_library"] = lib
    cfg["self_names"] = sel
    config.save(cfg)

    _emit({"config_path": str(config.CONFIG_PATH),
           "self_name_library": lib, "self_names": sel, "changes": notes},
          args.json,
          [f"配置文件：{config.CONFIG_PATH}",
           f"名称库（{len(lib)} 人）：{', '.join(lib) or '（空）'}",
           f"本次选用（{len(sel)} 人）：{', '.join(sel) or '（空）'}"]
          + ([f"· {n}" for n in notes] if notes else []))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
