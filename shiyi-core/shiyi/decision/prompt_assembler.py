"""PromptAssembler - Prompt装配器

职责：
- 根据预审结果+检索结果+对话历史+用户画像，动态组装prompt
- 必须注入对话历史（修复"好"字bug）
- system prompt包含角色设定、输出格式要求（回复+Fragments）
- 用户消息包含检索到的记忆片段、对话历史、当前输入

输入：
- pre_result: PreReviewResult，预审结果（区分+拆分+情感）
- fragments: List[Dict]，检索到的记忆片段
- conversation_history: List[Dict]，对话历史
- emotion_state: Dict，当前情感状态
- user_profile: Dict，用户画像

输出：
- messages: List[Dict]，组装好的messages列表（system+user）
"""

import json
import logging
import os
from typing import List, Dict, Any, Optional

from shiyi.common.types import PreReviewResult


logger = logging.getLogger(__name__)


# ═══ 跨平台 MEDIA 路径示例 ═══
if os.name == "nt":
    _MEDIA_EXAMPLE = "%USERPROFILE%\\.shiyi\\workspace\\xxx.txt"
else:
    _MEDIA_EXAMPLE = "~/.shiyi/workspace/xxx.txt"


# ═══ System Prompt 模板 ═══

SYSTEM_PROMPT_TEMPLATE = """你是史佚，一个拥有类人记忆能力的AI助手。

## 身份
你是史佚，一个拥有类人记忆能力的AI助手。你可以根据用户的习惯和偏好来称呼自己。

## 当前环境
{platform_context}

## 记忆能力
1. **记忆晶体（Fragment）**：你从对话中提取和存储记忆，每个晶体包含：
   - fact_kernel：核心事实（必须独立成立）
   - emotion_shell：情感标签
   - linked_to：关联的其他记忆

2. **记忆提取规则**：
   - ✅ 独立事实 → 新Fragment
   - ✅ AI主动补充 → 独立Fragment，无linked_to
   - ❌ fact_kernel不独立成立 → 过滤丢弃

3. **回复风格**：
   - 像朋友一样自然交流
   - 适当引用相关记忆
   - 简洁但不冷淡

## 输出格式
你的回复先是面向用户的自然语言正文，像朋友一样自然交流。

**工具优先规则：当系统提供了工具函数时，所有实际操作（文件读写、命令执行、网络请求等）必须通过调用工具函数完成，不得在文本中模拟。仅在工具不可用时，才在文本中说明。**

重要：文件写入必须使用 file_write 工具，不得使用 run_command 执行 echo/cat 等命令来创建文件。写入完成后，在回复中必须加上 "MEDIA:文件绝对路径" （如 MEDIA:""" + _MEDIA_EXAMPLE + """），网关会识别 MEDIA 标签并自动将文件发送给用户。

当对话中产生了值得记住的事实（用户个人信息、偏好、重要事件等），在回复末尾以 JSON 格式输出记忆片段：\n{\"fragments\": [{\"fact_kernel\": \"...\", \"emotion_shell\": {...}, \"scene_shell\": {...}}]}\n系统会自动解析并存储。如果没有值得记忆的内容，不要输出 JSON。

注意：调用工具获取实际结果后，不要再输出 JSON fragment，工具结果本身已验证。注意：和文件写入内容相关的，都是通过file_write工具完成，完成后不需要用json重复记录。"""


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
        pre_result: PreReviewResult,
        fragments: List[Dict[str, Any]],
        conversation_history: List[Dict[str, str]],
        query: str = "",
        emotion_state: Optional[Dict[str, Any]] = None,
        user_profile: Optional[Dict[str, Any]] = None,
        platform_context: str = "",
    ) -> List[Dict[str, str]]:
        """组装prompt消息列表

        Args:
            pre_result: 预审结果（区分+拆分+情感）
            fragments: 检索到的记忆片段列表
            conversation_history: 对话历史 [[role, content], ...]
            emotion_state: 当前情感状态
            user_profile: 用户画像
            platform_context: 动态平台上下文（渠道+可用工具等），如空则用默认

        Returns:
            组装好的messages列表
        """
        messages: List[Dict[str, str]] = []

        # 1. System消息 — 动态填入平台上下文
        ctx = platform_context or "当前渠道: 本地"
        system_content = self._system_prompt.replace("{platform_context}", ctx)
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
            pre_result=pre_result,
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
        pre_result: PreReviewResult,
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
