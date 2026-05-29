"""shiyi-core 史佚主引擎

类人记忆Agent核心引擎，提供对话、记忆、检索功能

v0.11.14 工具调用层（Tool Calling + 执行循环）

核心链路：
1. normalize - 输入标准化
2. intent - 意图识别（轻量LLM）
3. recall - 记忆检索
4. vector_search - 向量搜索
5. fuse - 多路结果融合去重
6. assemble - Prompt装配
7. decide - 主LLM调用
8. extract - Fragment提取
9. remember - Fragment存储
10. reply - 返回回复
"""

import os
import re
import json
import time
import logging
import threading
import queue
import uuid as _uuid
from pathlib import Path

from typing import Dict, Any, Optional, List, Generator

from shiyi.config import load_config
from shiyi.common.types import Fragment, IntentResult, ToolResult


# 模块级日志
_log = logging.getLogger("shiyi.engine")
from shiyi.common.interfaces import LLMProvider, EmbeddingProvider
from shiyi.common.errors import LLMUnavailableError
from shiyi.memory.engine import MemoryEngine

# 感知层模块
from shiyi.perception import (

    NormalizedInput,
    normalize,
    IntentEngine,
    IntentType,
    ConversationManager,
)

# 决策层模块
from shiyi.decision import DecideEngine, DecideResult
from shiyi.fuzi.experiment import FuziExperiment, ParamBridge
from shiyi.fuzi.learner import FuziLearner
from shiyi.fuzi.benchmarks import STANDARD_BENCHMARKS
from shiyi.core.clerk_registry import ClerkRegistry
from shiyi.core.task_tracker import TaskTracker
from shiyi.core.async_executor import AsyncClerkExecutor
from shiyi.core.kanban import KanbanBoard
from shiyi.core.steward import Steward, create_default_decompose_fn, create_default_aggregate_fn
from shiyi.core.skill_loader import SkillLoader
from shiyi.core.skill_registry import SkillRegistry
from shiyi.core.clerk_creator import ClerkCreator, start_clerk as _start_clerk
from shiyi.core.skill_hub import SkillHub

logger = logging.getLogger(__name__)


class Shiyi:
    """史佚主类 - 类人记忆Agent引擎"""
    
    def __init__(
        self,
        config_path: Optional[str] = None,
        llm_provider: Optional[LLMProvider] = None,
        conversation_db_path: str = "",
        embedding_provider: Optional[EmbeddingProvider] = None,
    ):
        """初始化史佚引擎
        
        Args:
            config_path: 配置文件路径，默认为 None 使用内置配置
            llm_provider: LLM调用提供者，用于IntentEngine和主LLM
            conversation_db_path: 对话历史数据库路径
            embedding_provider: Embedding服务提供者，用于向量搜索
        """
        self.config = load_config(config_path)
        
        # ═══ 存储依赖 ═══
        self._llm = llm_provider
        self._embedding = embedding_provider
        
        # 初始化记忆引擎
        memory_config = self.config.memory
        memory_kwargs = {
            "halflife_days": memory_config.get("halflife_days", 60.0),
            "emotion_multiplier": memory_config.get("emotion_multiplier", 1.5),
            "access_multiplier": memory_config.get("access_multiplier", 2.0),
            "max_hops": memory_config.get("max_hops", 2),
            "decay_per_hop": memory_config.get("decay_per_hop", 0.5),
            "hot_capacity": memory_config.get("hot_capacity", 50),
        }
        # 有 EmbeddingProvider 时启用向量索引
        if embedding_provider and embedding_provider.is_available():
            memory_kwargs["embedding_dim"] = 1024  # BGE-M3 维度
        self._memory = MemoryEngine(**memory_kwargs)
        
        # ═══ Phase 2 感知层初始化 ═══
        
        # 1. 感知层（Perception）- 输入标准化，直接使用normalize函数
        
        # 2. 意图识别引擎（IntentEngine）- LLM依赖注入
        self._intent_engine = IntentEngine(llm_provider=llm_provider)
        
        # 3. 对话历史管理器（ConversationManager）
        # 对话历史持久化路径
        conv_path = conversation_db_path or str(
            Path.home() / ".shiyi" / "data" / "conversations.db"
        )
        # 确保父目录存在
        os.makedirs(Path(conv_path).parent, exist_ok=True)
        self._conversation = ConversationManager(
            db_path=conv_path,
            window_size=10,
            max_tokens_per_turn=200,
        )
        
        # ═══ Phase 3 决策层初始化 ═══
        
        # 获取向量索引
        vector_index = self._memory.vector_index if hasattr(self._memory, 'vector_index') else None
        
        # 决策引擎 - 整合所有决策链路
        self._decide_engine = DecideEngine(
            memory_engine=self._memory,
            intent_engine=self._intent_engine,
            conversation_manager=self._conversation,
            llm_provider=llm_provider,
            embedding_provider=embedding_provider,
            vector_index=vector_index,
        )
        
        # 会话上下文
        self._current_conversation_id: str = "default"
        self._recent_intents: List[IntentResult] = []

        # ═══ Phase 4 Fuzi 初始化 ═══
        fuzi_db = str(
            Path.home() / ".shiyi" / "data" / "fuzi.db"
        )
        self._fuzi_bridge = ParamBridge(self._memory)
        self._fuzi_experiment = FuziExperiment(
            bridge=self._fuzi_bridge,
            db_path=fuzi_db,
        )
        self._fuzi_learner = FuziLearner(
            db_path=fuzi_db,
            parent_fuzi=self._fuzi_experiment,
        )

        # ═══ Phase 4.5 工具层初始化 ═══
        self._clerk_registry = ClerkRegistry()  # 统一工具+吏员注册中心
        self._task_tracker = TaskTracker()      # 吏员任务追踪
        self._load_clerks()                      # 自动加载磁盘上的吏员

        # ═══ Phase 5 Skill 层初始化 ═══
        self._skill_loader = SkillLoader(registry=self._clerk_registry)
        self._skill_loader.scan()  # 启动时扫描所有 Skills
        # Phase 1: 执行层注册中心（L0/L1/L2 分类）
        self._skill_registry = SkillRegistry(self._skill_loader)
        
        # Phase 6: Skill 市场（搜索 + 安装）
        self._skill_hub = SkillHub(
            skills_dir=Path.home() / ".shiyi" / "skills",
        )
        
        # 注入记忆引擎和对话管理器到 ClerkRegistry（供吏员调用记忆工具）
        self._clerk_registry._memory_engine = self._memory
        self._clerk_registry._conversation_manager = self._conversation
        
        # 注册 extract_memory 工具（让 LLM 通过 Function Calling 存记忆，而非在回复中内嵌 JSON）
        self._clerk_registry.register(
            name="extract_memory",
            description='从对话中提取值得记住的事实，存储到记忆库。当用户透露个人信息、偏好、重要事件时调用。纯呼应词（"好""嗯"）不调用。',
            parameters={
                "type": "object",
                "properties": {
                    "fact_kernel": {
                        "type": "string",
                        "description": "核心事实描述，必须是独立的、可理解的事实语句（如'用户叫张三，是Python程序员'）"
                    },
                    "emotion": {
                        "type": "string",
                        "description": "情感标签，自由命名（如开心、沮丧、焦虑、自豪、困惑、期待、失望、无聊……），根据对话上下文自行判断，不限于固定列表"
                    },
                    "linked_to": {
                        "type": "string",
                        "description": "关联的其他记忆ID，可空"
                    }
                },
                "required": ["fact_kernel"]
            },
            handler=lambda args: self._register_memory_tool(args),
        )
        
        # 注册 create_clerk 工具（让 LLM 通过 Function Calling 创建吏员）
        self._clerk_registry.register(
            name="create_clerk",
            description="创建一个新的吏员（专业工具执行者）。吏员是独立的 AI worker，可以有专属工具和技能。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "吏员名称（中英文均可，如'绘画师'、'代码审查员'）"
                    },
                    "desc": {
                        "type": "string",
                        "description": "吏员描述，说明其功能和用途"
                    },
                    "requires_llm": {
                        "type": "boolean",
                        "description": "是否需要 LLM 推理能力"
                    },
                    "api_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要的 API Key 名称列表（如 ['DEEPSEEK_API_KEY']）"
                    },
                    "model_name": {
                        "type": "string",
                        "description": "LLM 模型名称（如 deepseek-chat），requires_llm=true 时需要"
                    },
                    "provider": {
                        "type": "string",
                        "description": "LLM provider（如 deepseek、openai），默认 deepseek"
                    },
                    "base_url": {
                        "type": "string",
                        "description": "API 基础 URL（如 https://api.deepseek.com/v1）"
                    },
                },
                "required": ["name"],
            },
            handler=lambda args: self._create_clerk_tool(args),
        )
        self._clerk_registry.set_task_tracker(self._task_tracker)
        self._clerk_executor = AsyncClerkExecutor(self._task_tracker, max_workers=4)
        self._kanban = KanbanBoard()            # 多吏员任务看板（旧，由管家替代）
        self._steward = Steward(                # 管家 — 吏员协同调度器 v0.17.0
            clerk_registry=self._clerk_registry,
            executor=self._clerk_executor,
        )
        # Steward 的 LLM 回调（延迟注入，见 set_steward_llm）
        self._steward_llm_fn = None

        # ═══ 消息队列 + 后台处理线程 ═══
        self._msg_queue = queue.Queue()
        self._pending_replies = {}  # msg_id → {reply, batch_ids, error, ts}
        self._replies_lock = threading.Lock()
        self._worker_running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="shiyi-msg-worker", daemon=True
        )
        self._worker_thread.start()

        self._initialized = True
    
    @property
    def memory(self) -> MemoryEngine:
        """记忆引擎实例"""
        return self._memory
    
    @property
    def conversation(self) -> ConversationManager:
        """对话历史管理器实例"""
        return self._conversation
    
    @property
    def intent_engine(self) -> IntentEngine:
        """意图识别引擎实例"""
        return self._intent_engine
    
    @property
    def decide_engine(self) -> DecideEngine:
        """决策引擎实例"""
        return self._decide_engine
    
    @property
    def clerk_registry(self) -> ClerkRegistry:
        """统一工具+吏员注册中心"""
        return self._clerk_registry
    
    @property
    def task_tracker(self) -> TaskTracker:
        """吏员任务追踪器"""
        return self._task_tracker
    
    @property
    def skill_loader(self) -> SkillLoader:
        """Skill 加载器"""
        return self._skill_loader

    @property
    def route_model(self) -> str:
        """Skill 路由决策使用的模型名（默认 deepseek-v4-flash）"""
        return getattr(self, "_route_model", "deepseek-v4-flash")

    @route_model.setter
    def route_model(self, model: str) -> None:
        self._route_model = model

    @property
    def skill_registry(self) -> SkillRegistry:
        """Skill 执行注册中心"""
        return self._skill_registry
    
    @property
    def llm_available(self) -> bool:
        """检查主LLM是否可用"""
        return self._llm is not None and self._llm.is_available()
    
    @property
    def embedding_available(self) -> bool:
        """检查Embedding服务是否可用"""
        return self._embedding is not None and self._embedding.is_available()
    
    def _embedding_warning(self) -> str:
        """返回Embedding不可用时附加的提示信息"""
        return "\n\n⚠️ 语义搜索不可用（Embedding服务未配置），当前仅支持关键词搜索。建议配置SILICONFLOW_API_KEY以获得更好的记忆检索体验。"
    
    def set_conversation(self, conversation_id: str) -> None:
        """设置当前会话ID
        
        Args:
            conversation_id: 会话标识
        """
        self._current_conversation_id = conversation_id
    
    def process_input(
        self,
        raw_message: str,
        conversation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """处理输入（感知层完整流程）
        
        流程：
        1. normalize - 输入标准化
        2. intent_analysis - 意图识别
        3. 对话历史加载
        
        Args:
            raw_message: 原始输入
            conversation_id: 会话ID
            metadata: 附加元数据
            
        Returns:
            处理结果字典
        """
        conversation_id = conversation_id or self._current_conversation_id
        
        # 1. 输入标准化
        normalized = normalize(
            raw_message=raw_message,
            conversation_id=conversation_id,
            metadata=metadata,
        )
        
        # 2. 获取对话历史
        history = self._conversation.get_history_for_llm(conversation_id, max_turns=5)
        
        # 3. 意图识别（LLM不可用时抛出LLMUnavailableError）
        intent_result = self._intent_engine.analyze(
            query=normalized.normalized_text,
            history=history,
            recent_intents=self._recent_intents[-5:],  # 最近5轮意图
        )
        
        # 4. 更新最近意图记录
        self._recent_intents.append(intent_result)
        if len(self._recent_intents) > 20:
            self._recent_intents = self._recent_intents[-20:]

        # 5. 连续追问自动递增扩散跳数
        self._auto_increase_hops(intent_result)
        
        # 6. 记录对话历史（用户消息）
        self._conversation.add_message(
            conversation_id=conversation_id,
            role="user",
            content=normalized.normalized_text,
            intent=str(intent_result.intent),
        )
        
        return {
            "normalized": normalized,
            "intent": intent_result,
            "history": history,
            "conversation_id": conversation_id,
        }
    
    # ═══ 连续追问 hops 递增 ═══
    _RECALL_INTENTS = {"recall"}
    _HOPS_INCREMENT_THRESHOLD = 3  # 连续 N 轮追问后扩 hop
    
    def _auto_increase_hops(self, intent_result) -> None:
        """连续追问时自动递增扩散跳数
        
        规则：
        - 连续 3 轮 recall → max_hops +1
        - 任意非 recall 意图 → 重置 hops
        """
        intent_str = str(intent_result.intent) if hasattr(intent_result, 'intent') else ""
        
        if intent_str in self._RECALL_INTENTS:
            recent_recall = sum(
                1 for ir in self._recent_intents[-self._HOPS_INCREMENT_THRESHOLD:]
                if str(ir.intent) in self._RECALL_INTENTS
            )
            if recent_recall >= self._HOPS_INCREMENT_THRESHOLD:
                trigger = self._memory.trigger_engine
                trigger.increase_hops(1)
        else:
            # 非追问话题，重置扩散范围
            self._memory.trigger_engine.reset_hops()

    def _embed_content(self, text: str) -> Optional[List[float]]:
        """为文本生成 embedding 向量（有 provider 时）"""
        if self._embedding and self._embedding.is_available():
            try:
                return self._embedding.embed(text)
            except Exception:
                pass
        return None

    def _register_memory_tool(self, args: dict) -> dict:
        """extract_memory 工具 handler — 将 LLM 提取的事实存入记忆引擎"""
        fact_kernel = (args.get("fact_kernel") or "").strip()
        if not fact_kernel:
            return {"status": "skipped", "reason": "empty fact_kernel"}
        try:
            embedding = self._embed_content(fact_kernel)
            result = self._memory.remember(content=fact_kernel, embedding=embedding)
            return {
                "status": "stored" if result else "skipped",
                "fragment_id": result.get("fragment_id", "") if isinstance(result, dict) else "",
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)[:200]}

    def _create_clerk_tool(self, args: dict) -> dict:
        """create_clerk 工具 handler — 创建并注册新吏员"""
        try:
            creator = ClerkCreator()
            result = creator.create_non_interactive(**args)
            if not result.get("success"):
                return {"success": False, "error": result.get("error", "创建失败")}

            # 注册新吏员
            clerk_dir = result["clerk_dir"]
            from shiyi.core.clerk_connector import RemoteClerk
            proxy = RemoteClerk(
                server_script=str(Path(clerk_dir) / "mcp_server.py"),
                config_path=str(Path(clerk_dir) / "clerk.json"),
            )
            clerk_id = self._clerk_registry.register_clerk(proxy)
            return {
                "success": True,
                "clerk_id": result["clerk_id"],
                "clerk_dir": clerk_dir,
                "tools_registered": clerk_id,
            }
        except Exception as e:
            return {"success": False, "error": str(e)[:300]}

    def talk(
        self,
        user_input: str,
        conversation_id: Optional[str] = None,
        **kwargs
    ) -> str:
        """对话接口 - Phase 3 完整实现
        
        完整链路：
        1. normalize - 输入标准化
        2. intent - 意图识别（轻量LLM）
        3. recall - 记忆检索
        4. vector_search - 向量搜索
        5. fuse - 多路结果融合去重
        6. assemble - Prompt装配
        7. decide - 主LLM调用
        8. extract - Fragment提取
        9. remember - Fragment存储
        10. reply - 返回回复
        
        Args:
            user_input: 用户输入
            conversation_id: 会话ID
            **kwargs: 其他参数
            
        Returns:
            AI 回复文本
            
        Raises:
            LLMUnavailableError: LLM服务不可用时抛出
        """
        conversation_id = conversation_id or self._current_conversation_id
        
        # 感知层处理（标准化 + 记录历史）
        result = self.process_input(user_input, conversation_id)
        normalized = result["normalized"]
        
        # 决策引擎执行完整链路（复用感知层意图）
        # LLM不可用时抛出LLMUnavailableError，由上层捕获处理
        try:
            decide_result: DecideResult = self._decide_engine.decide(
                query=user_input,
                conversation_id=conversation_id,
                normalized_text=normalized.normalized_text,
                intent_result=result["intent"],
            )
        except LLMUnavailableError:
            raise
        
        # 记录AI回复到对话历史
        self._conversation.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=decide_result.reply,
            intent=decide_result.intent,
        )
        
        reply = decide_result.reply
        
        # Embedding不可用时在回复末尾附加提示
        if not self.embedding_available:
            reply += self._embedding_warning()
        
        return reply
    
    # ═══ Skill 相关接口 ═══
    
    def skill_list(self) -> List[Dict[str, Any]]:
        """列出所有已安装的 Skills
        
        Returns:
            Skill 列表
        """
        return self._skill_loader.list_skills()
    
    def skill_show(self, skill_id: str) -> Dict[str, Any]:
        """查看 Skill 详情
        
        Args:
            skill_id: Skill 标识
            
        Returns:
            Skill 详细信息
        """
        return self._skill_loader.show(skill_id)
    
    def skill_install(self, source: str, category: str = "") -> Dict[str, Any]:
        """安装 Skill
        
        Args:
            source: 来源（本地路径、远程等）
            category: 指定分类
            
        Returns:
            安装结果
        """
        return self._skill_loader.install(source, category)
    
    def skill_delete(self, skill_id: str) -> Dict[str, Any]:
        """删除 Skill
        
        Args:
            skill_id: Skill 标识
            
        Returns:
            删除结果
        """
        return self._skill_loader.delete(skill_id)
    
    def skill_rescan(self) -> int:
        """重新扫描 Skills 目录

        Returns:
            发现的 Skill 数量
        """
        return self._skill_loader.scan()

    # ═══ Phase 6: Skill 市场 ═══

    def skill_search(self, query: str, max_results: int = 20) -> Dict[str, Any]:
        """搜索技能市场

        在 Hermes Catalog、agentskills.io、skillsmp.com 等源中搜索。
        中文查询自动通过 LLM 翻译为英文搜索词。

        Args:
            query: 搜索关键词（中英文均可）
            max_results: 最大结果数

        Returns:
            {"success": bool, "entries": [...], "total_hits": int, "sources": [...]}
        """
        try:
            search_query = query

            # 中文查询 → LLM 翻译为英文搜索词
            if any('\u4e00' <= c <= '\u9fff' for c in query) and self._llm:
                try:
                    translated = self._translate_search_query(query)
                    if translated:
                        search_query = translated
                        logger.info("Skill search translated: '%s' → '%s'", query, translated)
                except Exception as e:
                    logger.warning("Skill search translation failed, using original: %s", e)

            result = self._skill_hub.search(search_query, max_results=max_results)
            # 标记已安装
            installed_ids = self._skill_hub.get_installed_ids()
            entries = []
            for e in result.to_list():
                e["installed"] = e["skill_id"] in installed_ids
                entries.append(e)
            return {
                "success": True,
                "query": query,
                "translated_query": search_query if search_query != query else "",
                "entries": entries,
                "total_hits": result.total_hits,
                "sources": result.sources_searched,
                "elapsed_ms": result.elapsed_ms,
            }
        except Exception as e:
            logger.error("Skill search failed: %s", e)
            return {"success": False, "error": str(e)}

    def _translate_search_query(self, query: str) -> str:
        """用 LLM 将中文搜索词翻译为英文关键词

        Args:
            query: 中文搜索词

        Returns:
            英文关键词字符串
        """
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": (
                "You are a search query translator. "
                "Translate the user's Chinese skill search query into concise English keywords. "
                "Output ONLY the keywords, no explanation, no markdown. "
                "Focus on technical terms and common skill names. "
                "Example: '视频制作' → 'video generation editing comfyui manim'"
            )},
            {"role": "user", "content": query},
        ]

        response = self._llm.chat(
            messages,
            model="deepseek-v4-flash",
            temperature=0,
            max_tokens=100,
        )

        translated = str(response).strip().strip('"').strip("'")
        # 去掉可能的 markdown/前缀
        translated = re.sub(r'^(Translation:|英文关键词:|Keywords:)\s*', '', translated, flags=re.IGNORECASE).strip()
        return translated if translated else ""

    def skill_hub_install(self, skill_id: str, source: str = "hermes") -> Dict[str, Any]:
        """从技能市场安装一个 Skill

        下载 SKILL.md → 保存到 ~/.shiyi/skills/ → 刷新 Registry。

        Args:
            skill_id: Skill 标识，如 "creative/ascii-video"
            source: 来源标识（默认 hermes）

        Returns:
            {"success": bool, "skill_id": str, "path": str, "message": str}
        """
        try:
            ok, msg = self._skill_hub.install(skill_id, source=source)
            if ok:
                # 刷新 SkillRegistry
                count = self._skill_registry.refresh()
                logger.info("SkillRegistry refreshed after install: %d skills", count)
            return {
                "success": ok,
                "skill_id": skill_id,
                "source": source,
                "path": msg if ok else "",
                "message": msg if ok else "",
                "error": "" if ok else msg,
            }
        except Exception as e:
            logger.error("Skill install failed: %s", e)
            return {"success": False, "skill_id": skill_id, "error": str(e)}

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 回复中提取 JSON 对象

        处理 markdown 代码块、嵌套 JSON 等常见格式。
        """
        if not text:
            return None
        raw = str(text)

        # 1. 去 markdown 代码块
        raw = re.sub(r'```(?:json)?\s*', '', raw)
        raw = re.sub(r'```\s*', '', raw)

        # 2. 提取 JSON 对象（支持嵌套，re.DOTALL 跨行）
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, TypeError):
                pass

        return None

    def skill_route(self, user_request: str) -> Dict[str, Any]:
        """LLM 路由：根据用户请求选择 Skill + 吏员

        构建 Skill + 吏员列表提示 → 交给 LLM 决策 → 返回结构化结果。
        不做事先的关键词过滤等任何前置处理——全部交给 LLM 判断。

        Args:
            user_request: 用户请求文本

        Returns:
            {"success": bool, "skill_id": str|None, "clerk_id": str|None, "task_description": str, "execution_mode": str}
        """
        if self._llm is None:
            return {"success": False, "error": "LLM not available"}

        try:
            # 获取吏员列表
            clerks_info = self._steward._get_clerks_info()

            # 构建 LLM 路由提示
            prompt = self._skill_registry.build_skill_clerk_prompt(clerks_info)

            messages: List[Dict[str, str]] = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_request},
            ]

            response = self._llm.chat(
                messages,
                model=self.route_model,
                temperature=0,
                max_tokens=1000,
            )

            # 提取 JSON - 从 message 对象提取 content 和 reasoning_content (DeepSeek模型)
            raw_text = ""
            if hasattr(response, 'get'):
                # 优先使用 content 字段
                content_val = response.get('content', '')
                reasoning = response.get('reasoning_content', '')
                
                # 如果 content 为空或被截断，使用 reasoning_content
                if content_val and len(content_val) > 10:
                    raw_text = content_val
                elif reasoning:
                    raw_text = reasoning
                    logger.info("Using reasoning_content for JSON extraction")
            else:
                raw_text = str(response)
            
            result = self._extract_json(raw_text)
            if result:
                result["success"] = True
                result.setdefault("skill_id", None)
                result.setdefault("clerk_id", None)
                result.setdefault("task_description", user_request[:80])
                logger.info(
                    "Skill routed by LLM: skill=%s, clerk=%s",
                    result["skill_id"], result["clerk_id"],
                )
                return result

            logger.warning("No JSON found in LLM routing response: %s", str(response)[:200])
            return {"success": False, "error": "Failed to parse LLM routing response"}

        except Exception as e:
            logger.error("Skill routing failed: %s", e)
            return {"success": False, "error": str(e)}

    def skill_route_and_dispatch(self, user_request: str) -> Dict[str, Any]:
        """一步完成：LLM 路由 → 吏员 dispatch

        所有请求统一走 dispatch，不再有 inject/none 分支。
        """
        # 1. LLM 路由
        route = self.skill_route(user_request)
        if not route.get("success"):
            return {"success": False, "error": route.get("error", "Skill routing failed")}

        skill_id = route.get("skill_id")
        clerk_id = route.get("clerk_id")

        # 无 skill + 无 clerk → 返回搜索提示
        if skill_id is None and clerk_id is None:
            return {
                "success": False,
                "error": "No matching skill or clerk",
                "not_needed": True,
                "search_hint": "我可以帮你在技能市场搜索相关 Skill，需要吗？",
            }

        # 有 skill 或无 skill 但有 clerk → dispatch 到吏员
        return self.skill_dispatch(
            skill_id=skill_id,
            user_request=route.get("task_description", user_request),
            clerk_id=clerk_id,
        )

    def skill_dispatch(
        self,
        skill_id: Optional[str],
        user_request: str,
        clerk_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将 Skill 分派给管家调度

        Args:
            skill_id: Skill 标识（可为 None，表示无需 skill 的吏员直接执行）
            user_request: 用户原始请求
            clerk_id: 指定吏员（可选，不传则自动匹配第一个可用吏员）

        Returns:
            执行结果
        """
        # 无 skill 时直接传空内容给吏员执行
        if skill_id is None:
            if clerk_id is None:
                return {"error": "Neither skill nor clerk specified", "success": False}
            result = self._steward.dispatch_skill(
                skill_id="",
                skill_content="",
                clerk_id=clerk_id,
                task_description=user_request,
            )
            return {
                **result,
                "skill_id": None,
                "clerk_id": clerk_id,
            }

        # 有 skill 时，加载 skill 内容传给吏员
        skill_info = self._skill_registry.get(skill_id)
        if skill_info is None:
            return {"error": f"Skill not found: {skill_id}", "success": False}

        # 获取第一个可用吏员
        if clerk_id is None:
            available_clerks = self._steward._get_clerks_info()
            if not available_clerks:
                return {"error": "No clerks available", "success": False}
            clerk_id = available_clerks[0].get("clerk_id")

        # 获取 Skill 内容
        skill_content = self._skill_registry.get_skill_content(skill_id)

        # 调度执行
        result = self._steward.dispatch_skill(
            skill_id=skill_id,
            skill_content=skill_content,
            clerk_id=clerk_id,
            task_description=f"执行 Skill {skill_info.name}: {user_request[:80]}",
        )

        result["success"] = result.get("state") == "done"
        result["skill_id"] = skill_id
        result["clerk_id"] = clerk_id
        return result

    # ═══ Phase 2: 管家配置/监控接口 ═══

    def steward_configure_clerk(self, clerk_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """管家：修改吏员配置"""
        return self._steward.configure_clerk(clerk_id, updates)

    def steward_rename_clerk(self, clerk_id: str, new_name: str) -> Dict[str, Any]:
        """管家：重命名吏员"""
        return self._steward.rename_clerk(clerk_id, new_name)

    def steward_delete_clerk(self, clerk_id: str) -> Dict[str, Any]:
        """管家：安全删除吏员"""
        return self._steward.delete_clerk(clerk_id)

    def steward_skill_assign(self, clerk_id: str, skills: List[str]) -> Dict[str, Any]:
        """管家：给吏员分配 Skill"""
        return self._steward.skill_assign(clerk_id, skills)

    def steward_start_clerk(self, clerk_id: str) -> Dict[str, Any]:
        """管家：启动吏员进程"""
        return self._steward.start_clerk(clerk_id)

    def steward_stop_clerk(self, clerk_id: str) -> Dict[str, Any]:
        """管家：停止吏员进程"""
        return self._steward.stop_clerk(clerk_id)

    def steward_clerk_health(self, clerk_id: Optional[str] = None) -> Dict[str, Any]:
        """管家：吏员健康检查"""
        return self._steward.clerk_health(clerk_id)

    def steward_start_monitor(self) -> None:
        """管家：启动后台监控"""
        return self._steward.start_monitor()

    def steward_stop_monitor(self) -> None:
        """管家：停止后台监控"""
        return self._steward.stop_monitor()

    def steward_clerk_alerts(self, clerk_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """管家：获取监控告警历史"""
        return self._steward.clerk_alerts(clerk_id)

    def steward_monitor_status(self) -> Dict[str, Any]:
        """管家：获取监控系统状态"""
        return self._steward.monitor_status()

    # ═══ Phase 3: 流水线编排接口 ═══

    def steward_create_pipeline(self, name: str, stages_def: List[Dict[str, Any]]) -> Dict[str, Any]:
        """管家：创建流水线"""
        pipeline = self._steward.create_pipeline(name, stages_def)
        return pipeline.to_dict()

    def steward_execute_pipeline(self, pipeline_id: str) -> Dict[str, Any]:
        """管家：执行流水线"""
        return self._steward.execute_pipeline(pipeline_id)

    def steward_get_pipeline(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """管家：获取流水线状态"""
        p = self._steward.get_pipeline(pipeline_id)
        return p.to_dict() if p else None

    def steward_list_pipelines(self) -> List[Dict[str, Any]]:
        """管家：列出所有流水线"""
        return self._steward.list_pipelines()
    
    def chat(
        self,
        user_input: str,
        conversation_id: Optional[str] = None,
        max_tool_rounds: int = 5,
        platform_context: str = "",
        **kwargs,
    ) -> str:
        """工具调用对话接口 — Phase 4 + 吏员系统

        在 talk() 基础上增加 Function Calling + 吏员工具执行循环。
        流程：
        1. talk() 正常流程（意图 → 检索 → LLM）
        2. 如果 LLM 返回 tool_call → 路由到吏员执行 → 结果回灌 LLM
        3. 循环直到 LLM 返回纯文本回复

        Args:
            user_input: 用户输入
            conversation_id: 会话ID
            max_tool_rounds: 最大工具调用轮数
            **kwargs: 其他参数

        Returns:
            AI 最终回复文本
        """
        # 从吏员注册中心获取工具 schema
        tools = self._clerk_registry.get_schemas()
        
        # 无已注册工具 → 直接 talk()
        if not tools:
            return self.talk(user_input, conversation_id=conversation_id)
        
        conversation_id = conversation_id or self._current_conversation_id
        
        # 感知层处理
        result = self.process_input(user_input, conversation_id)
        normalized = result["normalized"]
        intent_result = result["intent"]
        
        # 工具执行循环
        messages_extra = None
        final_reply = ""
        final_intent = str(intent_result.intent)
        
        for _round in range(max_tool_rounds):
            decide_result: DecideResult = self._decide_engine.decide(
                query=user_input,
                conversation_id=conversation_id,
                normalized_text=normalized.normalized_text,
                intent_result=intent_result,
                tools=tools,
                messages_extra=messages_extra,
                platform_context=platform_context,
            )
            
            final_intent = decide_result.intent
            
            # 无 tool_call → 返回回复
            if not decide_result.tool_call:
                final_reply = decide_result.reply
                break
            
            # 有 tool_call → 路由到吏员执行
            tc = decide_result.tool_call
            tool_msgs = []
            
            for call in tc.get("tool_calls", []):
                func = call.get("function", {})
                name = func.get("name", "")
                call_id = call.get("id", "")
                
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                
                # 吏员执行 + 任务追踪
                clerk_id = self._clerk_registry.tool_owner(name)
                task_id = self._task_tracker.start(
                    clerk_id=clerk_id or "unknown",
                    tool_name=name,
                    params=args,
                )
                
                exec_result = self._clerk_registry.execute(name, args)
                
                self._task_tracker.complete(task_id, exec_result)
                
                tool_msgs.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(exec_result, ensure_ascii=False),
                })
            
            # 追加 assistant tool_call 消息 + tool 结果
            assistant_msg = tc.get("message", {})
            if assistant_msg:
                messages_extra = ([assistant_msg] + tool_msgs 
                                  if messages_extra is None 
                                  else messages_extra + [assistant_msg] + tool_msgs)
            else:
                messages_extra = tool_msgs
        
        # 兜底：如果全部轮次都是 tool_call（不太可能），给个默认回复
        if not final_reply:
            final_reply = "抱歉，处理工具调用时遇到问题。"
        
        # 记录AI回复到对话历史
        self._conversation.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=final_reply,
            intent=final_intent,
        )
        
        # Embedding不可用时在回复末尾附加提示
        if not self.embedding_available:
            final_reply += self._embedding_warning()
        
        return final_reply

    def chat_stream(
        self,
        user_input: str,
        conversation_id: Optional[str] = None,
        platform_context: str = "",
    ):
        """流式对话接口 — 返回 token 生成器

        与 talk() 相同的预处理链路（意图→检索→融合），
        但主 LLM 调用改为流式，逐 token yield。
        不支持 Function Calling / Tool Calling。

        Args:
            user_input: 用户输入
            conversation_id: 会话ID
            platform_context: 动态平台上下文（渠道+吏员能力等）

        Yields:
            str: 文本片段（token）
        """
        conversation_id = conversation_id or self._current_conversation_id

        # 感知层处理（标准化 + 记录历史）
        result = self.process_input(user_input, conversation_id)
        normalized = result["normalized"]

        # 流式决策
        full_reply = ""
        embedding_warning = self._embedding_warning() if not self.embedding_available else ""
        
        for chunk_type, data in self._decide_engine.decide_stream(
            query=user_input,
            conversation_id=conversation_id,
            normalized_text=normalized.normalized_text,
            intent_result=result["intent"],
            platform_context=platform_context,
        ):
            if chunk_type == "token":
                full_reply += data
                yield data
            elif chunk_type == "done":
                # 记录AI回复到对话历史
                self._conversation.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=full_reply,
                    intent=data.intent,
                )
        
        # 流完成后附加embedding不可用提示
        if embedding_warning:
            for char in embedding_warning:
                yield char

    def chat_async(
        self,
        user_input: str,
        conversation_id: Optional[str] = None,
        max_tool_rounds: int = 5,
        **kwargs,
    ) -> str:
        """异步工具调用对话 — v0.14.0

        工具调用提交到线程池异步执行，不阻塞史佚主对话。
        返回包含 task_id 的摘要信息，可通过 task_status() 查询进度。

        Returns:
            JSON 格式的摘要（task_graph + 初始回复）
        """
        tools = self._clerk_registry.get_schemas()

        if not tools:
            return json.dumps({
                "mode": "talk",
                "reply": self.talk(user_input, conversation_id=conversation_id),
                "tasks": [],
            }, ensure_ascii=False)

        conversation_id = conversation_id or self._current_conversation_id

        # 感知层处理
        result = self.process_input(user_input, conversation_id)
        normalized = result["normalized"]
        intent_result = result["intent"]

        # 决策引擎执行（带 tools）
        decide_result: DecideResult = self._decide_engine.decide(
            query=user_input,
            conversation_id=conversation_id,
            normalized_text=normalized.normalized_text,
            intent_result=intent_result,
            tools=tools,
        )

        final_intent = decide_result.intent

        # 无 tool_call → 直接返回文本
        if not decide_result.tool_call:
            self._conversation.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=decide_result.reply,
                intent=final_intent,
            )
            return json.dumps({
                "mode": "talk",
                "reply": decide_result.reply,
                "tasks": [],
            }, ensure_ascii=False)

        # 有 tool_call → 异步提交任务到看板
        tc = decide_result.tool_call
        task_ids = []
        _kanban = self._kanban
        _registry = self._clerk_registry

        for call in tc.get("tool_calls", []):
            func = call.get("function", {})
            name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

            clerk_id = _registry.tool_owner(name) or "unknown"

            # 添加到看板
            task = _kanban.add_task(
                title=f"{name}: {str(args)[:60]}",
                clerk_id=clerk_id,
                tool_name=name,
                params=args,
            )

            def _do_execute(tn=name, pa=args, t=task):
                """异步执行并更新看板状态"""
                _kanban.mark_running(t.task_id)
                result = _registry.execute(tn, pa)
                if result.get("success") is False:
                    _kanban.mark_failed(t.task_id, result.get("error", "unknown"))
                else:
                    _kanban.mark_done(t.task_id, result)
                return result

            task_id = self._clerk_executor.submit(
                clerk_id=clerk_id,
                tool_name=name,
                params=args,
                execute_fn=_do_execute,
            )

            task_ids.append(task.task_id)

        # 记录到对话历史（先给个占位回复）
        self._conversation.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"[正在执行 {len(task_ids)} 个任务]",
            intent=final_intent,
        )

        return json.dumps({
            "mode": "async",
            "tasks": task_ids,
            "reply": f"已提交 {len(task_ids)} 个异步任务，正在进行中……",
            "task_summary": [
                {
                    "id": t.task_id,
                    "tool": t.tool_name,
                    "clerk": t.clerk_id,
                }
                for t in self._kanban.get_ready() + self._kanban.get_running()
                if t.task_id in task_ids
            ],
        }, ensure_ascii=False)

    # ═══════════════════════════════════════════
    # v0.14.0 异步任务 + 看板管理 API
    # ═══════════════════════════════════════════

    def task_status(self, task_id: str = "") -> Dict[str, Any]:
        """查询任务/看板状态

        Args:
            task_id: 为空时返回看板总览；指定时返回单任务详情

        Returns:
            状态字典
        """
        if task_id:
            t = self._kanban.get_by_id(task_id)
            if t is None:
                return {"error": "task not found"}
            return {
                "task_id": t.task_id,
                "title": t.title,
                "state": t.state,
                "clerk": t.clerk_id,
                "tool": t.tool_name,
                "result": t.result,
                "error": t.error,
                "parents": t.parents,
                "children": t.children,
            }
        return self._kanban.status()

    @property
    def kanban(self) -> KanbanBoard:
        """看板实例（旧，由管家替代）"""
        return self._kanban

    def _load_clerks(self) -> None:
        """自动加载 ~/.shiyi/clerks/ 下的所有吏员

        扫描 clerks/ 目录，对每个包含 clerk.json 和 mcp_server.py 的子目录调用 start_clerk()。
        跳过损坏的、缺失的、或 startup 失败的吏员。
        """
        clerks_dir = Path.home() / ".shiyi" / "clerks"
        if not clerks_dir.exists():
            logger.info("Clerks directory not found: %s", clerks_dir)
            return

        for entry in sorted(clerks_dir.iterdir()):
            if not entry.is_dir():
                continue

            clerk_id = entry.name
            clerk_json = entry / "clerk.json"
            mcp_py = entry / "mcp_server.py"

            if not clerk_json.exists():
                logger.warning("Skipping clerk %s: no clerk.json", clerk_id)
                continue
            if not mcp_py.exists():
                logger.warning("Skipping clerk %s: no mcp_server.py", clerk_id)
                continue

            try:
                result = _start_clerk(clerk_id, str(clerks_dir), registry=self._clerk_registry)
                if result.get("success"):
                    logger.info("Loaded clerk %s (%s), pid=%d, tools=%d",
                        clerk_id, result.get("name","?"), result.get("pid",0), result.get("tool_count",0))
                else:
                    logger.warning("Failed to load clerk %s: %s", clerk_id, result.get("error","?"))
            except Exception as e:
                logger.error("Error loading clerk %s: %s", clerk_id, e)
                continue

    @property
    def steward(self) -> Steward:
        """管家实例 — 吏员协同调度器"""
        return self._steward

    @property
    def clerk_executor(self) -> AsyncClerkExecutor:
        """异步执行器实例"""
        return self._clerk_executor
    
    def steward_status(self) -> Dict[str, Any]:
        """管家看板总览"""
        return self._steward.status()
    
    def steward_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """管家任务详情"""
        return self._steward.task_detail(task_id)

    def set_steward_llm(self, llm_fn: callable) -> None:
        """设置管家的 LLM 回调（由 shell 层调用）

        Args:
            llm_fn: LLM 调用函数，接收 messages 返回文本
        """
        self._steward_llm_fn = llm_fn
        if llm_fn:
            # 创建并注入回调
            self._steward._decompose_fn = create_default_decompose_fn(llm_fn)
            self._steward._aggregate_fn = create_default_aggregate_fn(llm_fn)

    def steward_run(self, user_request: str, auto_execute: bool = True) -> Dict[str, Any]:
        """创建并执行管家任务

        Args:
            user_request: 用户请求
            auto_execute: 是否自动执行，默认为 True

        Returns:
            任务信息字典
        """
        if not self._steward_llm_fn:
            return {
                "error": "Steward LLM 未初始化，请先调用 set_steward_llm()",
                "task_id": None,
            }

        task = self._steward.create_task(user_request)
        if auto_execute:
            self._steward.execute(task)
        return {
            "task_id": task.task_id,
            "state": task.state,
            "progress": task.progress,
            "summary": task.summary,
        }

    def recall(
        self,
        query: str,
        deep: bool = False,
        top_k: int = 10,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """记忆检索
        
        Args:
            query: 查询文本
            deep: 是否深度检索
            top_k: 返回数量
            **kwargs: 其他参数
            
        Returns:
            检索结果列表
        """
        results = self._memory.recall(query, deep=deep, top_k=top_k)
        
        # 刷新命中的 Fragment
        for result in results:
            if result.get("fragment"):
                fid = result["fragment"].id
                self._memory.refresh(fid)
        
        # 转换为可序列化格式
        return [
            {
                "id": r["fragment"].id,
                "fact_kernel": r["fragment"].fact_kernel,
                "score": r["score"],
                "source": r["source"],
                "emotion": {
                    "valence": r["fragment"].emotion_shell.valence,
                    "arousal": r["fragment"].emotion_shell.arousal,
                    "primary": r["fragment"].emotion_shell.primary,
                },
                "created_at": r["fragment"].time_shell.created_at,
            }
            for r in results
        ]
    
    def remember(self, content: str) -> bool:
        """记忆存储
        
        v1.0架构中 Fragment 提取在 talk() 内完成，
        remember 只接受单个 content 参数。
        
        Args:
            content: 记忆内容
            
        Returns:
            是否存储成功
        """
        embedding = self._embed_content(content)
        return self._memory.remember(content=content, embedding=embedding)
    
    def stats(self) -> Dict[str, Any]:
        """统计信息

        Returns:
            统计信息字典
        """
        memory_stats = self._memory.stats()
        return {
            **memory_stats,
            "conversation": {
                "current_conversation_id": self._current_conversation_id,
                "recent_intent_count": len(self._recent_intents),
            },
        }

    # ═══════════════════════════════════════════
    # Phase 4: Fuzi + Entity API
    # ═══════════════════════════════════════════

    def entity_view(self, entity_name: str) -> Dict[str, Any]:
        """聚合实体画像

        通过关系引擎检索所有包含此实体的 Fragment，
        聚合生成综合画像。

        Args:
            entity_name: 实体名称（人名/事物名等）

        Returns:
            {entity, fragments: [...], summary: {domains, emotions, timeline}}
        """
        results = self._memory.recall(entity_name, deep=False, top_k=30)
        fragments = [r["fragment"] for r in results if r.get("fragment")]

        domains = set()
        emotions = []
        timeline = []

        for f in fragments:
            if f.scene_shell.domain:
                domains.add(f.scene_shell.domain)
            if f.emotion_shell.primary:
                emotions.append({
                    "emotion": f.emotion_shell.primary,
                    "valence": f.emotion_shell.valence,
                })
            if f.time_shell.created_at:
                timeline.append({
                    "fact": f.fact_kernel[:80],
                    "time": f.time_shell.created_at,
                })

        return {
            "entity": entity_name,
            "fragment_count": len(fragments),
            "domains": sorted(domains),
            "dominant_emotion": max(
                set(e["emotion"] for e in emotions),
                key=lambda x: sum(1 for e in emotions if e["emotion"] == x),
            ) if emotions else "未知",
            "emotion_count": len(emotions),
            "timeline": sorted(timeline, key=lambda t: t["time"], reverse=True)[:10],
        }

    def run_benchmark(self, benchmark_set=None) -> Dict[str, Any]:
        """执行 Fuzi 基准测试

        Args:
            benchmark_set: 自定义测试集（默认使用标准 12 条）

        Returns:
            基准测试报告
        """
        cases = benchmark_set or STANDARD_BENCHMARKS

        def recall_wrapper(query: str):
            results = self._memory.recall(query, top_k=10)
            return [
                {
                    "fact_kernel": r["fragment"].fact_kernel,
                    "score": r["score"],
                }
                for r in results
            ]

        result = self._fuzi_experiment.run_benchmark(cases, recall_wrapper)
        # 记录实验快照
        self._fuzi_experiment.record_experiment(
            score=result["score"],
            notes=f"benchmark {result['passed']}/{result['total_cases']} passed",
        )
        return result

    def record_feedback(self, feedback_type, conversation_id="", query="", details=None) -> str:
        """记录用户反馈信号

        Args:
            feedback_type: 反馈类型（"ask_more"/"corrected"/"silence"/"like"/"dislike"）
            conversation_id: 对话 ID
            query: 触发反馈的查询
            details: 附加信息

        Returns:
            信号 ID
        """
        return self._fuzi_learner.record_feedback(
            feedback_type=feedback_type,
            conversation_id=conversation_id,
            query=query,
            details=details,
        )

    def get_fuzi_report(self, include_raw: bool = False) -> Dict[str, Any]:
        """获取夫子安全报告

        Args:
            include_raw: 是否包含原始数据（生产环境应为 False）

        Returns:
            报告字典
        """
        engine_stats = self._memory.stats()
        learner_signals = self._fuzi_learner.aggregate_period(days=7)

        report = self._fuzi_experiment.safe_report(
            include_raw=include_raw,
            engine_stats=engine_stats,
            learner_signals=learner_signals,
        )

        # 追加学习建议
        report["evolution_suggestions"] = self._fuzi_learner.propose_evolution(
            signals=learner_signals
        )

        return report
    
    # ═══ 消息队列 + 后台处理 ═══
    
    def enqueue_message(
        self, text: str, msg_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """将消息加入处理队列，立即返回 msg_id"""
        msg_id = msg_id or str(_uuid.uuid4())
        conv_id = conversation_id or self._current_conversation_id
        self._msg_queue.put((msg_id, text, conv_id))
        return msg_id
    
    def get_pending_replies(self, since_msg_id: str = "") -> List[Dict[str, Any]]:
        """获取已处理完成的回复（不清除，由调用方按需取）
        
        Args:
            since_msg_id: 返回此 ID 之后的回复（不含此 ID）
        
        Returns:
            [{"msg_id": ..., "reply": ..., "batch_ids": [...], "error": ...}, ...]
        """
        with self._replies_lock:
            all_ids = list(self._pending_replies.keys())
        
        try:
            idx = all_ids.index(since_msg_id) if since_msg_id else -1
        except ValueError:
            idx = -1
        new_ids = all_ids[idx + 1:]
        
        results = []
        with self._replies_lock:
            for mid in new_ids:
                entry = self._pending_replies.get(mid)
                if entry:
                    results.append({"msg_id": mid, **entry})
        
        # 清理旧回复（保留最近 100 条）
        with self._replies_lock:
            if len(self._pending_replies) > 100:
                old_ids = all_ids[:-50]
                for oid in old_ids:
                    self._pending_replies.pop(oid, None)
        
        return results
    
    def _worker_loop(self) -> None:
        """后台消息处理循环 daemon 线程
        
        收集策略：出队第一条后立即排空队列，把同 conv 的消息
        合并为一个 batch，一次性 LLM 调用处理。
        """
        while self._worker_running:
            try:
                msg_id, text, conv_id = self._msg_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            
            # 1. 收集同 conv 的等待消息（包括自身）
            batched_texts = [text]
            batched_ids = [msg_id]
            
            # 排空队列，收集同 conv 最多 10 条（等待窗口5秒）
            window_end = time.time() + 5.0
            while time.time() < window_end and len(batched_ids) < 10:
                try:
                    remaining = window_end - time.time()
                    if remaining <= 0:
                        break
                    mid, mtxt, mconv = self._msg_queue.get(timeout=min(0.5, remaining))
                    if mconv == conv_id:
                        batched_ids.append(mid)
                        batched_texts.append(mtxt)
                        # 又来一条，重置窗口再等
                        window_end = time.time() + 2.0
                    else:
                        # 不同对话的消息放回去
                        self._msg_queue.put((mid, mtxt, mconv))
                        break
                except queue.Empty:
                    continue  # 窗口未到，继续等
            
            # 2. 合并处理
            combined = "\n".join(batched_texts)
            error = None
            try:
                reply = self._process_sync(combined, conv_id)
            except Exception as e:
                reply = ""
                error = str(e)
            
            # 3. 存入 pending_replies
            with self._replies_lock:
                self._pending_replies[batched_ids[0]] = {
                    "reply": reply,
                    "batch_ids": batched_ids,
                    "error": error,
                    "ts": time.time(),
                }
    
    def _process_sync(self, text: str, conv_id: str) -> str:
        """同步处理单条消息，收集流式输出为完整字符串
        直接调用 talk() 走完整链路
        """
        try:
            _log.info(f"[sync] 处理消息: conv={conv_id}, len={len(text)}")
            result = self.talk(text, conversation_id=conv_id)
            _log.info(f"[sync] 处理完成: conv={conv_id}, len={len(result)}")
            return result
        except Exception:
            _log.exception(f"[sync] 处理异常: conv={conv_id}")
            return ""
    
    def close(self) -> None:
        """关闭引擎，保存数据"""
        self._worker_running = False
        if self._memory:
            self._memory.close()
        if self._conversation:
            self._conversation.close()
        if self._fuzi_experiment:
            self._fuzi_experiment.close()
        if self._fuzi_learner:
            self._fuzi_learner.close()
        self._clerk_executor.shutdown(wait=False)
