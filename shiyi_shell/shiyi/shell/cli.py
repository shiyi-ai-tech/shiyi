"""shiyi-shell CLI 命令行工具

普通用户：shiyi webui
开发者模式：shiyi --dev <command>
"""

import argparse
import sys
import json
import os
from pathlib import Path

from shiyi.engine import Shiyi
from shiyi.shell.llm_caller import create_llm_caller
from shiyi.shell.embedding_caller import create_embedding_caller


# 版本号 — 动态导入，避免硬编码不同步
from shiyi.shell import __version__ as VERSION

# ── 需要开发者模式的命令 ──
_DEV_COMMANDS = {"talk", "chat", "recall", "remember", "stats", "version", "entity", "fuzi", "clerk", "skill"}


def _load_env() -> None:
    """加载 .env 文件（与 webui.py 一致）

    项目根 .env 无条件覆盖环境变量；~/.shiyi/.env 只在环境变量未设置时补充。
    这确保史佚自身配置优先于其他进程（如 Hermes）设置的同名环境变量。
    """
    env_paths = [
        Path(__file__).parent.parent.parent.parent / ".env",   # 项目根目录
        Path.home() / ".shiyi" / ".env",                         # 用户配置目录
    ]
    for i, env_path in enumerate(env_paths):
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        key = key.strip()
                        val = val.strip()
                        if not (val and key):
                            continue
                        if i == 0:
                            # 项目 .env — 无条件覆盖
                            os.environ[key] = val
                        elif key not in os.environ:
                            # 用户 .env — 仅补充未设置的键
                            os.environ[key] = val


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

    # ═══ 注册默认吏员（与 webui.py 一致） ═══
    if shiyi:
        try:
            clerk_path = Path(__file__).parent / "clerk-default"
            mcp_script = clerk_path / "mcp_server.py"
            if mcp_script.exists():
                from shiyi.core.clerk_connector import RemoteClerk
                remote_clerk = RemoteClerk(
                    server_script=str(mcp_script),
                    config_path=str(clerk_path / "clerk.json"),
                )
                shiyi.clerk_registry.register_clerk(remote_clerk)
                print(f"远程吏员已注册: {remote_clerk.config.clerk_id}")
            else:
                sys.path.insert(0, str(clerk_path))
                from worker import ClerkWorker
                local_clerk = ClerkWorker(str(clerk_path / "clerk.json"))
                shiyi.clerk_registry.register_clerk(local_clerk)
                print(f"本地吏员已注册: {local_clerk.config.clerk_id}")
        except Exception as e:
            print(f"吏员注册失败（无工具模式）: {e}")

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


def cmd_clerk_validate(clerk_dir: str, smoke_test: bool = False) -> None:
    """校验吏员目录是否符合规范"""
    from shiyi.core.clerk_validator import validate_clerk_dir
    result = validate_clerk_dir(clerk_dir, smoke_test=smoke_test)
    print(result.report(verbose=True))


def cmd_skill_install(skill_path: str, generate: bool = False, output: str = None) -> None:
    """分析 SKILL.md 并建议吏员连接"""
    import os
    from shiyi.core.skill_installer import (
        parse_skill_md, load_clerk_repo, AnalysisResult, MatchResult,
        compute_match_score, generate_clerk_template, generate_worker_stub
    )

    # 取得 clerk repo 路径 (相对 cli.py 位置)
    cli_dir = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.join(cli_dir, "..", "..", "..", "..", "shiyi-shell", "shiyi", "shell")
    if not os.path.exists(repo):
        repo = os.path.join(cli_dir, "..")  # fallback

    # 1. 解析技能
    skill = parse_skill_md(skill_path)
    if not skill.name and not skill.raw_body:
        print(f"❌ 无法解析 SKILL.md: {skill_path}")
        return

    # 2. 加载吏员
    clerks = load_clerk_repo(repo)

    # 3. 匹配
    matches = []
    for clerk in clerks:
        mr = compute_match_score(skill, clerk)
        if mr.score > 0:
            matches.append(mr)
    matches.sort(key=lambda m: m.score, reverse=True)

    result = AnalysisResult(skill, matches)

    # 4. 判定建议
    if matches and matches[0].score >= 0.3:
        result.suggested_action = "connect"
        result.suggested_clerk = matches[0]
    else:
        result.suggested_action = "new"

    # 5. 若建议新建，生成模板
    if result.suggested_action == "new":
        tpl = generate_clerk_template(skill, output or "")
        result.clerk_template = tpl

    # 6. 输出
    print(result.summary())

    # 7. --generate 输出完整 clerk.json
    if generate and result.clerk_template:
        print("\n── clerk.json ──")
        import json as _json
        display = {k: v for k, v in result.clerk_template.items() if not k.startswith("_")}
        print(_json.dumps(display, indent=2, ensure_ascii=False))

    # 8. --output 生成完整吏员骨架
    if output and result.clerk_template:
        out_dir = os.path.abspath(output)
        os.makedirs(out_dir, exist_ok=True)

        # clerk.json
        cj = {k: v for k, v in result.clerk_template.items() if not k.startswith("_")}
        cj["created_at"] = ""
        cj_path = os.path.join(out_dir, "clerk.json")
        with open(cj_path, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(cj, f, indent=2, ensure_ascii=False)
        print(f"\n✅ clerk.json → {cj_path}")

        # worker.py
        worker_content = generate_worker_stub(skill, cj["clerk_id"])
        wp = os.path.join(out_dir, "worker.py")
        with open(wp, "w", encoding="utf-8") as f:
            f.write(worker_content)
        print(f"✅ worker.py   → {wp}")

        # mcp_server.py (从 clerk-default 复制)
        cli_dir2 = os.path.dirname(os.path.abspath(__file__))
        default_mcp = os.path.join(cli_dir2, "clerk-default", "mcp_server.py")
        if os.path.exists(default_mcp):
            import shutil
            dest_mcp = os.path.join(out_dir, "mcp_server.py")
            shutil.copy(default_mcp, dest_mcp)
            print(f"✅ mcp_server.py → {dest_mcp}")
        else:
            print("⚠️  未找到 mcp_server.py 模板，请手动添加")

        print(f"\n📦 吏员骨架已生成到: {out_dir}")
        print("   下一步: 编辑 clerk.json 和 worker.py 补全实现")


def cmd_steward_status(shiyi: Shiyi) -> None:
    """管家看板总览"""
    status = shiyi.steward_status()
    print(json.dumps(status, indent=2, ensure_ascii=False))


def cmd_steward_task(task_id: str, shiyi: Shiyi) -> None:
    """管家任务详情"""
    detail = shiyi.steward_task(task_id)
    if detail is None:
        print(f"任务不存在: {task_id}")
        return
    print(json.dumps(detail, indent=2, ensure_ascii=False))


def cmd_steward_run(request: str, shiyi: Shiyi, auto_execute: bool = True) -> None:
    """创建并执行管家任务"""
    # 设置 LLM 回调（如果还没有）
    if not hasattr(shiyi, '_steward_llm_fn') or shiyi._steward_llm_fn is None:
        llm = create_llm_caller()
        llm_fn = lambda msgs: llm.chat(msgs, temperature=0.3, max_tokens=4000)
        shiyi.set_steward_llm(llm_fn)

    result = shiyi.steward_run(request, auto_execute=auto_execute)

    if "error" in result and result.get("task_id") is None:
        print(f"错误: {result['error']}")
        return

    print(f"任务ID: {result['task_id']}")
    print(f"状态: {result['state']}")
    if result.get('progress'):
        p = result['progress']
        print(f"进度: {p.get('done', 0)}/{p.get('total', 0)}")
    if result.get('summary'):
        print(f"\n汇总:\n{result['summary']}")


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

    # ── 吏员命令 ──
    clerk_p = subparsers.add_parser("clerk", help="吏员管理", add_help=False,
                                     description="[开发者] 吏员管理")
    clerk_sub = clerk_p.add_subparsers(dest="clerk_command", help="吏员子命令")
    
    # clerk validate - 校验吏员目录
    clerk_val = clerk_sub.add_parser("validate", help="校验吏员目录", add_help=False,
                                      description="[开发者] 校验吏员目录")
    clerk_val.add_argument("clerk_dir", help="吏员目录路径")
    clerk_val.add_argument("--smoke-test", action="store_true",
                           help="导入 worker.py 做运行时烟雾测试")
    
    # clerk create - 创建吏员
    clerk_create = clerk_sub.add_parser("create", help="创建吏员", add_help=False,
                                         description="[开发者] 创建新吏员")
    clerk_create.add_argument("name", nargs="?", default="", help="吏员名称")
    clerk_create.add_argument("--desc", "-d", default="", help="吏员描述")
    clerk_create.add_argument("--tools", "-t", default="", help="工具列表（逗号分隔）")
    clerk_create.add_argument("--no-interactive", action="store_true", help="非交互模式")
    clerk_create.add_argument("--output", "-o", default="", help="输出目录")
    
    # clerk delete - 删除吏员
    clerk_delete = clerk_sub.add_parser("delete", help="删除吏员", add_help=False,
                                         description="[开发者] 删除吏员")
    clerk_delete.add_argument("identifier", help="吏员名称或ID")
    clerk_delete.add_argument("--force", "-f", action="store_true", help="强制删除")
    
    # clerk list - 列出吏员
    clerk_sub.add_parser("list", help="列出所有吏员", add_help=False,
                         description="[开发者] 列出吏员")

    # ── 技能安装向导 ──
    skill_p = subparsers.add_parser("skill", help="Skill管理", add_help=False,
                                     description="[开发者] Skill管理")
    skill_sub = skill_p.add_subparsers(dest="skill_command", help="技能子命令")
    
    # skill install - 分析并安装 Skill
    skill_ins = skill_sub.add_parser("install", help="安装Skill", add_help=False,
                                      description="[开发者] 安装Skill")
    skill_ins.add_argument("source", nargs="?", default="", help="来源路径")
    skill_ins.add_argument("--category", "-c", default="", help="指定分类")
    
    # skill list - 列出已安装的 Skills
    skill_sub.add_parser("list", help="列出已安装的Skills", add_help=False,
                          description="[开发者] 列出Skills")
    
    # skill show - 查看 Skill 详情
    skill_show_p = skill_sub.add_parser("show", help="查看Skill详情", add_help=False,
                                         description="[开发者] 查看Skill详情")
    skill_show_p.add_argument("skill_id", help="Skill标识")
    
    # skill delete - 删除 Skill
    skill_del_p = skill_sub.add_parser("delete", help="删除Skill", add_help=False,
                                        description="[开发者] 删除Skill")
    skill_del_p.add_argument("skill_id", help="Skill标识")
    
    # skill scan - 重新扫描
    skill_sub.add_parser("scan", help="重新扫描Skills目录", add_help=False,
                          description="[开发者] 重新扫描")
    
    # skill check - 检查Skill依赖
    skill_check_p = skill_sub.add_parser("check", help="检查Skill依赖", add_help=False,
                                          description="[开发者] 检查Skill依赖")
    skill_check_p.add_argument("skill_id", nargs="?", default="", help="Skill标识或路径")

    # ── 管家命令 ──
    steward_p = subparsers.add_parser("steward", help="管家 — 吏员协同调度看板", add_help=False,
                                       description="[开发者] 管家调度看板")
    steward_sub = steward_p.add_subparsers(dest="steward_command", help="管家子命令")
    steward_sub.add_parser("status", help="看板总览", add_help=False)
    steward_task_p = steward_sub.add_parser("task", help="任务详情", add_help=False)
    steward_task_p.add_argument("task_id", help="管家任务ID")
    steward_run_p = steward_sub.add_parser("run", help="执行管家任务", add_help=False)
    steward_run_p.add_argument("request", help="用户请求（用引号包裹）", nargs="+")
    steward_run_p.add_argument("--no-execute", action="store_true", help="仅创建任务，不自动执行")

    # ── 网关命令 ──
    gateway_p = subparsers.add_parser("gateway", help="启动平台网关", add_help=False,
                                       description="[开发者] 启动平台网关（飞书/微信等）")
    gateway_sub = gateway_p.add_subparsers(dest="gateway_command", help="网关子命令")
    gateway_feishu = gateway_sub.add_parser("feishu", help="启动飞书网关", add_help=False,
                                             description="[开发者] 启动飞书网关")
    # gateway_sub.add_parser("wechat", help="启动微信网关", add_help=False)  # 后续

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
        print("  clerk validate <目录>         校验吏员目录")
        print("  clerk create [名称]            创建新吏员")
        print("  clerk delete <名称或ID>      删除吏员")
        print("  clerk list                     列出所有吏员")
        print("  skill install <SKILL.md>       分析技能并建议吏员连接")
        print("  skill check <name>             检查Skill依赖状态")
        print("  steward status                 管家看板总览")
        print("  steward task <ID>             管家任务详情")
        print("  steward run <请求>            管家执行用户请求")
        print("  gateway feishu                启动飞书网关")
    print()


def main() -> None:
    _load_env()  # 加载 .env 环境变量（与 webui.py 一致）
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

    # ── 不需要引擎的纯分析命令 ──
    if args.command == "clerk":
        from shiyi.core.clerk_creator import (
            ClerkCreator, delete_clerk, list_clerks
        )
        
        if hasattr(args, 'clerk_command') and args.clerk_command == "validate":
            cmd_clerk_validate(args.clerk_dir, smoke_test=args.smoke_test)
        
        elif args.clerk_command == "create":
            # 吏员创建
            creator = ClerkCreator(args.output) if args.output else ClerkCreator()
            
            if args.no_interactive or args.name:
                # 非交互模式
                tools = []
                if args.tools:
                    for t in args.tools.split(","):
                        t = t.strip()
                        if t:
                            tools.append({
                                "name": t,
                                "description": f"执行{t}操作",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string", "description": "操作"}
                                    },
                                    "required": ["action"]
                                }
                            })
                
                result = creator.create_non_interactive(
                    name=args.name or "custom",
                    desc=args.desc,
                    tools=tools if tools else None
                )
            else:
                # 交互模式
                result = creator.create_interactive()
            
            if result.get("success"):
                print(f"✅ {result['message']}")
                print(f"   目录: {result['clerk_dir']}")
            else:
                print(f"❌ {result.get('error', '创建失败')}")
        
        elif args.clerk_command == "delete":
            # 吏员删除 — 支持按名称或ID删除
            identifier = args.identifier
            clerk_id = None
            
            # 先尝试按clerk_id直接匹配
            clerks = list_clerks()
            for c in clerks:
                if c["clerk_id"] == identifier:
                    clerk_id = identifier
                    break
            
            # 按名称匹配
            if not clerk_id:
                matched = [c for c in clerks if c["name"] == identifier]
                if len(matched) == 1:
                    clerk_id = matched[0]["clerk_id"]
                elif len(matched) > 1:
                    print(f"❌ 多个吏员同名 '{identifier}'，请用clerk_id指定:")
                    for c in matched:
                        print(f"  {c['clerk_id']} - {c['name']}")
                    return
                else:
                    print(f"❌ 未找到吏员: {identifier}")
                    return
            
            if not args.force:
                # 找到对应name用于确认提示
                target = next((c for c in clerks if c["clerk_id"] == clerk_id), {})
                display = f"{target.get('name', clerk_id)} ({clerk_id})"
                confirm = input(f"确认删除吏员 '{display}'? [y/N]: ").strip().lower()
                if confirm not in ("y", "yes"):
                    print("取消删除")
                    return
            
            result = delete_clerk(clerk_id)
            if result.get("success"):
                print(f"✅ {result['message']}")
            else:
                print(f"❌ {result.get('error', '删除失败')}")
        
        elif args.clerk_command == "list":
            # 列出吏员
            clerks = list_clerks()
            if not clerks:
                print("暂无吏员")
            else:
                print(f"共有 {len(clerks)} 个吏员:\n")
                for c in clerks:
                    status = "✓" if c["enabled"] else "✗"
                    print(f"  [{status}] {c['name']}")
                    print(f"      ID: {c['clerk_id']}")
                    print(f"      工具: {c['tool_count']} 个")
                    print()
        
        else:
            print("用法: shiyi --dev clerk {validate,create,delete,list}", file=sys.stderr)
        return
    if args.command == "skill":
        # skill 命令需要引擎来访问 SkillLoader
        shiyi = _init_shiyi()
        if args.skill_command == "install":
            if not args.source:
                print("用法: shiyi --dev skill install <source> [--category <category>]", file=sys.stderr)
                return
            result = shiyi.skill_install(args.source, category=args.category)
            if result.get("success"):
                print(f"✅ {result.get('message')}")
            else:
                print(f"❌ {result.get('message')}")
        elif args.skill_command == "list":
            skills = shiyi.skill_list()
            if not skills:
                print("暂无已安装的 Skills")
                return
            print(f"已安装 {len(skills)} 个 Skills:\n")
            for s in skills:
                print(f"  📦 {s['skill_id']}")
                print(f"     名称: {s['name']}")
                if s['description']:
                    print(f"     描述: {s['description'][:60]}...")
                print()
        elif args.skill_command == "show":
            result = shiyi.skill_show(args.skill_id)
            if "error" in result:
                print(f"❌ {result['error']}")
            else:
                print(f"=== Skill: {result['name']} ({result['skill_id']}) ===")
                print(f"分类: {result['category']}")
                print(f"描述: {result['description']}")
                print(f"路径: {result['path']}")
                if result.get('triggers'):
                    print(f"触发词: {', '.join(result['triggers'])}")
                if result.get('requires'):
                    print(f"依赖: {result['requires']}")
                print("\n--- 内容 ---")
                print(result.get('content', ''))
        elif args.skill_command == "delete":
            result = shiyi.skill_delete(args.skill_id)
            if result.get("success"):
                print(f"✅ {result.get('message')}")
            else:
                print(f"❌ {result.get('message')}")
        elif args.skill_command == "scan":
            count = shiyi.skill_rescan()
            print(f"扫描完成，发现 {count} 个 Skills")
        elif args.skill_command == "check":
            # 检查 Skill 依赖
            from shiyi.core.skill_installer import check_skill_dependencies
            from shiyi.core.clerk_creator import list_clerks
            
            skill_id = args.skill_id
            if not skill_id:
                print("用法: shiyi --dev skill check <skill_id|path>", file=sys.stderr)
                return
            
            # 获取已注册的吏员
            clerks = list_clerks()
            registered = [c["clerk_id"] for c in clerks]
            
            # 尝试解析为路径或skill_id
            skill_path = skill_id
            if not os.path.exists(skill_id):
                # 尝试在skills目录查找
                skills_dir = Path.home() / ".shiyi" / "skills"
                for root, _, files in os.walk(skills_dir):
                    if "SKILL.md" in files:
                        skill_md = Path(root) / "SKILL.md"
                        if skill_id in str(skill_md):
                            skill_path = str(skill_md)
                            break
            
            report = check_skill_dependencies(skill_path, registered)
            print(report.summary())
        else:
            print("用法: shiyi --dev skill {install,list,show,delete,scan,check}", file=sys.stderr)
        return

    # ── 开发者模式（需要引擎） ──
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
    elif args.command == "steward":
        if args.steward_command == "status":
            cmd_steward_status(shiyi)
        elif args.steward_command == "task":
            cmd_steward_task(args.task_id, shiyi)
        elif args.steward_command == "run":
            request = " ".join(args.request)
            cmd_steward_run(request, shiyi, auto_execute=not args.no_execute)
        else:
            print("用法: shiyi --dev steward {status,task,run}", file=sys.stderr)
    elif args.command == "gateway":
        if args.gateway_command == "feishu":
            from shiyi.shell.gateway.run import run as gateway_run
            gateway_run("feishu", shiyi)
        else:
            print("用法: shiyi gateway {feishu}", file=sys.stderr)


if __name__ == "__main__":
    main()
