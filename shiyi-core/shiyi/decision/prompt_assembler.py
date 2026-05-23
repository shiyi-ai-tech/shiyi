"""PromptAssembler - Prompt装配器

职责：
- 根据意图+检索结果+对话历史+用户画像，动态组装prompt
- 必须注入对话历史（修复"好"字bug）
- system prompt包含角色设定、输出格式要求（回复+Fragments）
- 用户消息包含检索到的记忆片段、对话历史、当前输入

输入：
- intent_result: IntentResult，意图分析结果
- fragments: List[Dict]，检索到的记忆片段
- conversation_history: List[Dict]，对话历史
- emotion_state: Dict，当前情感状态
- user_profile: Dict，用户画像

输出：
- messages: List[Dict]，组装好的messages列表（system+user）
"""

import json
import logging
from typing import List, Dict, Any, Optional

from shiyi.common.types import IntentResult, Fragment


logger = logging.getLogger(__name__)


# ═══ System Prompt 模板 ═══

SYSTEM_PROMPT_TEMPLATE = """你是史佚，一个拥有类人记忆能力的AI助手。

## 身份
你是史佚，一个拥有类人记忆能力的AI助手。你可以根据用户的习惯和偏好来称呼自己。

## 核心能力
1. **记忆能力**：你能够从对话中提取和存储记忆碎片（Fragment），每个碎片包含：
   - fact_kernel：核心事实（必须独立成立）
   - emotion_shell：情感标签
   - linked_to：关联的其他记忆

2. **记忆提取规则**：
   - ✅ 独立事实 → 新Fragment
   - ✅ AI主动补充(D) → 独立Fragment，无linked_to
   - ❌ 纯呼应("好""嗯") → 仅存对话历史，不产Fragment
   - ❌ fact_kernel不独立成立 → 过滤丢弃

3. **回复风格**：
   - 像朋友一样自然交流
   - 适当引用相关记忆
   - 简洁但不冷淡

## 输出格式
你的回复只需包含对话正文，像朋友一样自然交流。无需在回复中夹带任何 JSON 或标记。

**记忆存储方式**：当对话中产生了值得记住的事实（用户个人信息、偏好、重要事件等），调用 `extract_memory` 工具进行存储。
如果没有值得记忆的内容，不要调用该工具。纯呼应词（"好""嗯"等）不产生记忆。"""

# ═══ 纯呼应词列表 ═══

ECHO_WORDS = {
    "好", "嗯", "行", "是", "啊", "哦", "哈", "哎", "噢", "哇",
    "嗯嗯", "好好", "哈哈", "对对", "是是", "行行", "ok", "OK",
    "好嘞", "好哒", "好的", "收到", "了解", "知道了", "明白",
    "可以", "没问题", "好呀", "嗯嗯嗯", "好的好的",
}


class PromptAssembler:
    """Prompt装配器"""
    
    def __init__(self, system_prompt: Optional[str] = None):
        """初始化Prompt装配器
        
        Args:
            system_prompt: 自定义system prompt，默认使用内置模板
        """
        self._system_prompt = system_prompt or SYSTEM_PROMPT_TEMPLATE
    
    def assemble(
        self,
        intent_result: IntentResult,
        fragments: List[Dict[str, Any]],
        conversation_history: List[Dict[str, str]],
        query: str = "",
        emotion_state: Optional[Dict[str, Any]] = None,
        user_profile: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """组装prompt消息列表
        
        流程：
        1. 构建system消息（角色设定+输出格式）
        2. 注入对话历史作为上下文
        3. 注入检索到的记忆片段
        4. 添加当前用户输入
        
        Args:
            intent_result: 意图分析结果
            fragments: 检索到的记忆片段列表
            conversation_history: 对话历史 [[role, content], ...]
            emotion_state: 当前情感状态
            user_profile: 用户画像
            
        Returns:
            组装好的messages列表
        """
        messages: List[Dict[str, str]] = []
        
        # 1. System消息
        system_content = self._system_prompt
        
        # 如果有情感状态，添加到system prompt
        if emotion_state:
            emotion_text = self._format_emotion_state(emotion_state)
            system_content += f"\n\n## 当前情感状态\n{emotion_text}"
        
        # 如果有用户画像，添加到system prompt
        if user_profile:
            profile_text = self._format_user_profile(user_profile)
            system_content += f"\n\n## 用户画像\n{profile_text}"
        
        messages.append({
            "role": "system",
            "content": system_content,
        })
        
        # 2. 构建用户消息（包含记忆片段和对话历史）
        user_content = self._build_user_message(
            intent_result=intent_result,
            fragments=fragments,
            conversation_history=conversation_history,
            query=query,
        )
        
        messages.append({
            "role": "user",
            "content": user_content,
        })
        
        return messages
    
    def _format_emotion_state(self, emotion_state: Dict[str, Any]) -> str:
        """格式化情感状态"""
        parts = []
        if "valence" in emotion_state:
            parts.append(f"效价: {emotion_state['valence']}")
        if "arousal" in emotion_state:
            parts.append(f"唤醒度: {emotion_state['arousal']}")
        if "primary" in emotion_state:
            parts.append(f"主要情感: {emotion_state['primary']}")
        return ", ".join(parts) if parts else "中性"
    
    def _format_user_profile(self, user_profile: Dict[str, Any]) -> str:
        """格式化用户画像"""
        parts = []
        if "name" in user_profile:
            parts.append(f"名字: {user_profile['name']}")
        if "interests" in user_profile:
            parts.append(f"兴趣: {', '.join(user_profile['interests'])}")
        if "preferences" in user_profile:
            parts.append(f"偏好: {user_profile['preferences']}")
        return "\n".join(parts) if parts else "未知"
    
    def _build_user_message(
        self,
        intent_result: IntentResult,
        fragments: List[Dict[str, Any]],
        conversation_history: List[Dict[str, str]],
        query: str = "",
    ) -> str:
        """构建用户消息"""
        parts = []
        
        # 1. 对话历史（必须注入，修复"好"字bug）
        if conversation_history:
            history_text = self._format_conversation_history(conversation_history)
            parts.append(f"## 对话历史\n{history_text}")
        
        # 2. 检索到的记忆片段
        if fragments:
            fragments_text = self._format_fragments(fragments)
            parts.append(f"## 相关记忆\n{fragments_text}")
        else:
            parts.append("## 相关记忆\n暂无相关记忆")
        
        # 3. 当前输入
        query_text = query or "（无输入）"
        parts.append(f"## 当前输入\n用户说：{query_text}\n请回复用户的输入，并在回复末尾根据需要提取记忆片段。")
        
        return "\n\n".join(parts)
    
    def _format_conversation_history(
        self,
        conversation_history: List[Dict[str, str]],
    ) -> str:
        """格式化对话历史
        
        注意：必须注入对话历史，否则"好"字无法理解上下文
        """
        lines = []
        for msg in conversation_history[-10:]:  # 最近10轮
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # 截断超长内容
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"{role}: {content}")
        
        if not lines:
            return "（暂无历史）"
        
        return "\n".join(lines)
    
    def _format_fragments(self, fragments: List[Dict[str, Any]]) -> str:
        """格式化记忆片段"""
        if not fragments:
            return "暂无相关记忆"
        
        lines = []
        for i, f in enumerate(fragments[:5], 1):  # 最多5条
            fact_kernel = f.get("fact_kernel", "")
            if len(fact_kernel) > 100:
                fact_kernel = fact_kernel[:100] + "..."
            
            score = f.get("score", 0)
            emotion = f.get("emotion_shell", {})
            emotion_primary = emotion.get("primary", "中性") if isinstance(emotion, dict) else "中性"
            
            lines.append(f"{i}. [{score:.2f}] {fact_kernel} (情感: {emotion_primary})")
        
        return "\n".join(lines)
    
    def is_echo_only(self, text: str) -> bool:
        """判断是否为纯呼应词（不产Fragment）"""
        cleaned = text.strip()
        return cleaned in ECHO_WORDS
