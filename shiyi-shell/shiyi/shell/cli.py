"""shiyi-shell CLI 命令行工具

普通用户：shiyi webui
开发者模式：shiyi --dev <command>
"""

import argparse
import sys
import json

from shiyi.engine import Shiyi
from shiyi.shell.llm_caller import create_llm_caller
from shiyi.shell.embedding_caller import create_embedding_caller


# 版本号 — 动态导入，避免硬编码不同步
from shiyi.shell import __version__ as VERSION

# ── 需要开发者模式的命令 ──
_DEV_COMMANDS = {"talk", "chat", "recall", "remember", "stats", "version", "entity", "fuzi"}


def _init_shiyi():
    """初始化引擎"""
    shiyi = None
    try:
        llm = create_llm_caller()
        embedding = create_embedding_caller()
        shiyi = Shiyi(llm_provider=llm, embedding_provider=embedding)
    except ValueError as e:
        print(f"警告: {e}", file=sys.stderr)
        try:
            shiyi = Shiyi(llm_provider=None, embedding_provider=None)
        except Exception as e2:
            print(f"初始化失败: {e2}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"初始化失败: {e}", file=sys.stderr)
        sys.exit(1)
    return shiyi


def cmd_talk(user_input: str, shiyi: Shiyi, verbose: bool = False) -> None:
    try:
        reply = shiyi.talk(user_input)
        if verbose:
            print(f"[回复] {reply}")
            print(f"[LLM可用] {shiyi.llm_available}")
        else:
            print(reply)
    except Exception as e:
        print(f"对话失败: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()


def cmd_chat(user_input: str, shiyi: Shiyi, verbose: bool = False) -> None:
    result = shiyi.chat(user_input)
    if verbose:
        print(f"[会话] {result['conversation_id']}")
        print(f"[意图] {result['intent']['type']} (置信度: {result['intent']['confidence']:.2f})")
        print(f"[需要检索] {result['intent']['needs_retrieval']}")
        if result['retrieval_results']:
            print(f"[检索结果] ({len(result['retrieval_results'])} 条)")
            for i, r in enumerate(result['retrieval_results'][:3]):
                if 'error' not in r:
                    print(f"  {i+1}. {r.get('fact_kernel', '')[:50]}... (分数: {r.get('score', 0):.2f})")
    else:
        print(f"意图: {result['intent']['type']} | 复杂度: {result['normalized']['complexity']}")


def cmd_recall(query: str, deep: bool, shiyi: Shiyi) -> None:
    result = shiyi.recall(query, deep=deep)
    if result:
        print(f"检索到 {len(result)} 条记忆:")
        for i, r in enumerate(result):
            print(f"\n{i+1}. {r['fact_kernel'][:100]}...")
            print(f"   分数: {r['score']:.3f}")
            print(f"   情感: {r['emotion']['primary']}")
    else:
        print("未找到相关记忆")


def cmd_remember(content: str, shiyi: Shiyi) -> None:
    result = shiyi.remember(content)
    print(f"记忆存储: {'成功' if result else '失败'}")


def cmd_stats(shiyi: Shiyi) -> None:
    result = shiyi.stats()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_version() -> None:
    print(VERSION)


def cmd_entity(entity_name: str, shiyi: Shiyi) -> None:
    result = shiyi.entity_view(entity_name)
    print(f"实体: {entity_name}")
    print(f"相关片段: {result['fragment_count']}")
    print(f"领域: {', '.join(result['domains']) if result['domains'] else '无'}")
    print(f"主导情感: {result['dominant_emotion']}")
    if result['timeline']:
        print(f"时间线 ({len(result['timeline'])}条):")
        for t in result['timeline']:
            print(f"  [{t['time'][:10]}] {t['fact']}")


def cmd_fuzi_bench(shiyi: Shiyi) -> None:
    print("执行 Fuzi 标准基准测试（12条）...")
    result = shiyi.run_benchmark()
    print(f"\n通过: {result['passed']}/{result['total_cases']}")
    print(f"综合评分: {result['score']}")
    for case in result['case_results']:
        status = "✓" if case['passed'] else "✗"
        print(f"  {status} {case['intent']:8s} | {case['query'][:30]:30s} | score={case['score']}")


def cmd_fuzi_report(shiyi: Shiyi) -> None:
    report = shiyi.get_fuzi_report(include_raw=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))


def _build_parser():
    """构建参数解析器"""
    parser = argparse.ArgumentParser(
        prog="shiyi",
        description="史佚 - 类人记忆Agent",
    )
    parser.add_argument("--version", action="version", version=f"shiyi {VERSION}",
                        help="显示版本号")
    parser.add_argument("--dev", action="store_true", help="开发者模式（解锁全部命令）")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ── 公开命令 ──
    pub_group = parser.add_argument_group("公开命令")
    subparsers.add_parser("webui", help="启动 Web Chat UI", add_help=False)

    # ── 开发者命令（--dev 解锁） ──
    dev_group = parser.add_argument_group("开发者命令（需 --dev）")

    talk_p = subparsers.add_parser("talk", help="完整对话（决策链路）", add_help=False,
                                    description="[开发者] 完整对话")
    talk_p.add_argument("input", help="用户输入")
    talk_p.add_argument("-v", "--verbose", action="store_true", help="详细输出")

    chat_p = subparsers.add_parser("chat", help="简单对话（感知+意图+检索）", add_help=False,
                                    description="[开发者] 简单对话")
    chat_p.add_argument("input", help="用户输入")
    chat_p.add_argument("-v", "--verbose", action="store_true", help="详细输出")

    recall_p = subparsers.add_parser("recall", help="记忆检索", add_help=False,
                                      description="[开发者] 记忆检索")
    recall_p.add_argument("query", help="查询文本")
    recall_p.add_argument("--deep", action="store_true", help="深度检索")

    remember_p = subparsers.add_parser("remember", help="记忆存储", add_help=False,
                                        description="[开发者] 记忆存储")
    remember_p.add_argument("content", help="记忆内容")

    subparsers.add_parser("stats", help="统计信息", add_help=False,
                          description="[开发者] 统计信息")
    subparsers.add_parser("version", help="版本号", add_help=False,
                          description="[开发者] 版本号")

    entity_p = subparsers.add_parser("entity", help="实体聚合画像", add_help=False,
                                      description="[开发者] 实体聚合画像")
    entity_p.add_argument("entity_name", help="实体名称")

    fuzi_p = subparsers.add_parser("fuzi", help="夫子学习优化系统", add_help=False,
                                    description="[开发者] 夫子系统")
    fuzi_sub = fuzi_p.add_subparsers(dest="fuzi_command", help="夫子子命令")
    fuzi_sub.add_parser("bench", help="执行标准基准测试（12条）", add_help=False)
    fuzi_sub.add_parser("report", help="生成Fuzi安全报告", add_help=False)

    return parser


def _print_help(parser: argparse.ArgumentParser, dev: bool = False):
    """自定义帮助输出"""
    print("史佚 ShiYi — 类人记忆Agent\n")
    print("用法:")
    print("  shiyi webui                  启动 Web Chat UI")
    print("  shiyi --version              显示版本号")
    print("  shiyi --help                 显示帮助")
    if dev:
        print("  shiyi --dev <command>        开发者模式")
        print()
        print("开发者命令 (--dev):")
        print("  talk <输入>                  完整对话")
        print("  chat <输入>                  简单对话")
        print("  recall <查询>                记忆检索")
        print("  remember <内容>              记忆存储")
        print("  stats                        统计信息")
        print("  version                      版本号")
        print("  entity <名称>                实体聚合画像")
        print("  fuzi bench                   基准测试")
        print("  fuzi report                  安全报告")
    print()


def main() -> None:
    parser = _build_parser()

    # 手动解析 --dev（在 subparser 之前）
    dev_mode = "--dev" in sys.argv
    args = parser.parse_args()

    if not args.command:
        _print_help(parser, dev=dev_mode)
        return

    # ── 公开命令短路 ──
    if args.command == "webui":
        from shiyi.shell.webui import main as webui_main
        webui_main()
        return

    # ── 纯输出命令不需要引擎 ──
    if args.command == "version":
        cmd_version()
        return

    # ── 非公开命令必须 --dev ──
    if not dev_mode:
        print(f"错误: 'shiyi {args.command}' 需要开发者模式 (--dev)", file=sys.stderr)
        print(f"提示: 普通用户请使用  shiyi webui", file=sys.stderr)
        sys.exit(1)

    # ── 开发者模式 ──
    shiyi = _init_shiyi()

    if args.command == "talk":
        cmd_talk(args.input, shiyi, args.verbose)
    elif args.command == "chat":
        cmd_chat(args.input, shiyi, args.verbose)
    elif args.command == "recall":
        cmd_recall(args.query, args.deep, shiyi)
    elif args.command == "remember":
        cmd_remember(args.content, shiyi)
    elif args.command == "stats":
        cmd_stats(shiyi)
    elif args.command == "version":
        cmd_version()
    elif args.command == "entity":
        cmd_entity(args.entity_name if hasattr(args, 'entity_name') else args.input, shiyi)
    elif args.command == "fuzi":
        if args.fuzi_command == "bench":
            cmd_fuzi_bench(shiyi)
        elif args.fuzi_command == "report":
            cmd_fuzi_report(shiyi)
        else:
            print("用法: shiyi --dev fuzi {bench,report}", file=sys.stderr)


if __name__ == "__main__":
    main()
