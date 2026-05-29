"""FragmentExtractor - Fragment提取器

职责：
- 从主LLM回复中提取Fragment
- v1.0架构：主LLM单次输出回复+Fragments（不再独立调用consolidate）

Fragment生成规则：
- ✅ 独立事实 → 新Fragment
- ✅ AI主动补充(D) → 独立Fragment, 无linked_to
- ❌ fact_kernel不独立成立 → 过滤丢弃

方法：extract(reply_text) -> List[FragmentDict]
解析LLM输出中的```json```块或标记区域
"""

import json
import re
import logging
from typing import List, Dict, Any, Optional


logger = logging.getLogger(__name__)


# ═══ Fragment 数据结构 ═══

FragmentDict = Dict[str, Any]


class FragmentExtractor:
    """Fragment提取器"""
    
    def __init__(self):
        """初始化Fragment提取器"""
        pass
    
    def extract(self, reply_text: str) -> List[FragmentDict]:
        """从LLM回复中提取Fragment
        
        解析策略：
        1. 查找 ```json ... ``` 代码块
        2. 查找 ``` ... ``` 代码块
        3. 查找 [ ... ] 数组
        4. 如果都不是，返回空列表
        
        Args:
            reply_text: LLM回复文本
            
        Returns:
            提取的Fragment列表
        """
        if not reply_text or not reply_text.strip():
            return []
        
        # 1. 尝试解析 ```json ``` 块
        fragments = self._extract_from_json_block(reply_text)
        if fragments is not None:
            return self._filter_fragments(fragments)
        
        # 2. 尝试解析 ``` ``` 块
        fragments = self._extract_from_code_block(reply_text)
        if fragments is not None:
            return self._filter_fragments(fragments)
        
        # 3. 尝试直接解析数组
        fragments = self._extract_from_raw_text(reply_text)
        if fragments is not None:
            return self._filter_fragments(fragments)
        
        # 4. 没有找到JSON，返回空
        logger.debug(f"No fragments found in reply: {reply_text[:100]}...")
        return []
    
    def _extract_from_json_block(self, text: str) -> Optional[List[FragmentDict]]:
        """从 ```json ``` 块中提取"""
        # 匹配 ```json ... ``` 或 ```json\n...```
        pattern = r'```json\s*([\s\S]*?)\s*```'
        matches = re.findall(pattern, text)
        
        for match in matches:
            try:
                data = json.loads(match.strip())
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                continue
        
        return None
    
    def _extract_from_code_block(self, text: str) -> Optional[List[FragmentDict]]:
        """从 ``` ``` 块中提取"""
        # 匹配 ``` ... ```（非json）
        pattern = r'```\s*([\s\S]*?)\s*```'
        matches = re.findall(pattern, text)
        
        for match in matches:
            # 跳过已经是json的情况
            if match.strip().startswith('json'):
                continue
            
            # 尝试解析
            try:
                data = json.loads(match.strip())
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                continue
        
        return None
    
    def _extract_from_raw_text(self, text: str) -> Optional[List[FragmentDict]]:
        """从原始文本中提取JSON数组"""
        # 查找最后一个 [...] 数组
        bracket_count = 0
        start_idx = -1
        
        for i, char in enumerate(text):
            if char == '[':
                if bracket_count == 0:
                    start_idx = i
                bracket_count += 1
            elif char == ']':
                bracket_count -= 1
                if bracket_count == 0 and start_idx >= 0:
                    array_text = text[start_idx:i+1]
                    try:
                        data = json.loads(array_text)
                        if isinstance(data, list):
                            return data
                    except json.JSONDecodeError:
                        pass
                    start_idx = -1
        
        return None
    
    def _filter_fragments(self, fragments: List[FragmentDict]) -> List[FragmentDict]:
        """过滤无效的Fragment
        
        过滤规则：
        1. 必须有 fact_kernel
        2. fact_kernel 必须独立成立（不能是空字符串或纯符号）
        """
        valid_fragments = []
        
        for f in fragments:
            # 1. 检查是否有 fact_kernel
            fact_kernel = f.get("fact_kernel", "")
            if not fact_kernel or not isinstance(fact_kernel, str):
                continue
            
            # 2. 检查 fact_kernel 是否独立成立
            cleaned = fact_kernel.strip()
            # 与 _is_meaningless 阈值保持一致：至少3个有效字符
            if len(cleaned) < 3:
                continue
            
            # 3. 检查是否为纯符号/无意义内容
            if self._is_meaningless(cleaned):
                continue
            
            # 4. 通过验证，添加到结果
            valid_fragments.append({
                "fact_kernel": cleaned,
                "emotion_shell": f.get("emotion_shell", {
                    "valence": 0.0,
                    "arousal": 0.0,
                    "primary": "中性",
                }),
                "linked_to": f.get("linked_to", ""),
            })
        
        return valid_fragments
    
    def _is_meaningless(self, text: str) -> bool:
        """判断是否为无意义内容"""
        # 去除空白后的文本
        cleaned = text.strip()
        
        # 纯符号
        if re.match(r'^[\s\W]+$', cleaned):
            return True
        
        # 太短（小于3个字符）
        if len(cleaned) < 3:
            return True
        
        # 纯数字
        if re.match(r'^[\d\s,，.。]+$', cleaned):
            return True
        
        return False
    
    def extract_reply_only(self, full_text: str) -> str:
        """仅提取对话回复部分（不含JSON块）
        
        Args:
            full_text: LLM完整回复
            
        Returns:
            仅对话回复文本
        """
        if not full_text:
            return ""
        
        # 移除 ```json ... ``` 块
        text = re.sub(r'```json\s*[\s\S]*?\s*```', '', full_text)
        
        # 移除 ``` ... ``` 块
        text = re.sub(r'```\s*[\s\S]*?\s*```', '', text)
        
        # 移除独立 JSON 数组块（仅当 [ 出现在行首时，避免吃掉 markdown 链接）
        text = re.sub(r'(?:^|\n)\s*\[[\s\S]*?\]\s*(?:\n|$)', '\n', text)
        
        # 清理多余空白
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        return text.strip()
