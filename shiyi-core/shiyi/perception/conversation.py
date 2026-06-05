"""对话历史管理 - 对话原文持久化、滑动窗口、上下文注入

职责：
- 对话原文持久化到SQLite
- 滑动窗口：最近N轮（默认10轮）
- 超长截断：token估算，每轮最多200字
- 对话历史必须给主LLM上下文，否则"好"字无法理解

注意：
- 纯呼应词只进对话历史，不产Fragment
- 对话历史需要给主LLM上下文
"""

import os
import sqlite3
import threading
import time
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """对话消息"""
    conversation_id: str
    role: str              # user / assistant
    content: str
    timestamp: str = ""
    intent: str = ""       # 意图类型
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


class ConversationManager:
    """对话历史管理器"""
    
    def __init__(self, db_path: str = "", window_size: int = 10, max_tokens_per_turn: int = 200):
        """初始化对话历史管理器
        
        Args:
            db_path: SQLite数据库路径，为空时默认 ~/.shiyi/data/conversations.db
            window_size: 滑动窗口大小，默认10轮
            max_tokens_per_turn: 每轮最大token估算（中文约2字符=1token）
        """
        if not db_path:
            db_path = str(
                __import__("pathlib").Path.home() / ".shiyi" / "data" / "conversations.db"
            )
        self.db_path = db_path
        self.window_size = window_size
        self.max_chars_per_turn = max_tokens_per_turn * 2  # 粗略估算
        self._lock = threading.Lock()
        
        # 确保父目录存在（非内存数据库时）
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
    
    def _init_db(self) -> None:
        """初始化数据库表"""
        cursor = self._conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                intent TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversation_id 
            ON conversations(conversation_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp 
            ON conversations(conversation_id, timestamp)
        """)
        
        self._conn.commit()
    
    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        intent: str = "",
    ) -> int:
        """添加消息到对话历史
        
        Args:
            conversation_id: 会话ID
            role: 角色 (user/assistant)
            content: 消息内容
            intent: 意图类型
            
        Returns:
            消息ID
        """
        # 截断超长内容
        truncated = self._truncate_content(content)
        
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=truncated,
            timestamp=datetime.now().isoformat(),
            intent=intent,
        )
        
        with self._lock:
            cursor = self._conn.cursor()
            
            cursor.execute("""
                INSERT INTO conversations 
                (conversation_id, role, content, timestamp, intent, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                message.conversation_id,
                message.role,
                message.content,
                message.timestamp,
                message.intent,
                datetime.now().isoformat(),
            ))
            
            msg_id = cursor.lastrowid
            self._conn.commit()
            
            logger.debug(f"Added message to conversation {conversation_id}: role={role}, len={len(content)}")
            return msg_id
    
    def get_recent(
        self,
        conversation_id: str,
        limit: int = 20,
        roles: Optional[List[str]] = None,
    ) -> List[Message]:
        """获取最近的对话消息
        
        Args:
            conversation_id: 会话ID
            limit: 返回数量上限
            roles: 过滤角色 (如 ["user", "assistant"])
            
        Returns:
            消息列表
        """
        with self._lock:
            cursor = self._conn.cursor()
            
            query = """
                SELECT conversation_id, role, content, timestamp, intent
                FROM conversations
                WHERE conversation_id = ?
            """
            params = [conversation_id]
            
            if roles:
                placeholders = ','.join('?' * len(roles))
                query += f" AND role IN ({placeholders})"
                params.extend(roles)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            # 反转顺序（按时间正序）
            messages = [
                Message(
                    conversation_id=row[0],
                    role=row[1],
                    content=row[2],
                    timestamp=row[3],
                    intent=row[4] or "",
                )
                for row in reversed(rows)
            ]
            
            return messages
    
    def get_context_window(
        self,
        conversation_id: str,
        window_size: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """获取上下文窗口（给LLM用）
        
        Args:
            conversation_id: 会话ID
            window_size: 窗口大小，默认使用self.window_size
            
        Returns:
            [{"role": "user", "content": "..."}, ...]
        """
        size = window_size or self.window_size
        
        # 获取最近2*window_size条消息（user+assistant）
        messages = self.get_recent(conversation_id, limit=size * 2)
        
        # 只取最后window_size轮（每轮包含user+assistant）
        # 逻辑：取最近的size个user消息及其后的assistant
        result = []
        user_count = 0
        target_user_count = min(size, 10)  # 最多10轮对话
        
        for msg in reversed(messages):
            if msg.role == "user":
                user_count += 1
                if user_count > target_user_count:
                    break
            
            result.insert(0, {
                "role": msg.role,
                "content": msg.content,
            })
        
        return result
    
    def get_history_for_llm(
        self,
        conversation_id: str,
        max_turns: int = 5,
    ) -> List[Dict[str, str]]:
        """获取给LLM的对话历史（更精简的格式）
        
        Args:
            conversation_id: 会话ID
            max_turns: 最大轮数（每轮=1个user+1个assistant）
            
        Returns:
            对话历史列表
        """
        # 获取最近N*2条消息（user+assistant）
        messages = self.get_recent(conversation_id, limit=max_turns * 2)
        
        return [
            {
                "role": msg.role,
                "content": msg.content,
            }
            for msg in messages
        ]
    
    def get_turn_count(self, conversation_id: str) -> int:
        """获取对话轮数（user消息数量）"""
        with self._lock:
            cursor = self._conn.cursor()
            
            cursor.execute("""
                SELECT COUNT(*) FROM conversations
                WHERE conversation_id = ? AND role = 'user'
            """, (conversation_id,))
            
            count = cursor.fetchone()[0]
            
            return count
    
    def clear_conversation(self, conversation_id: str) -> int:
        """清空指定对话的所有消息
        
        Returns:
            删除的消息数量
        """
        with self._lock:
            cursor = self._conn.cursor()
            
            cursor.execute("""
                DELETE FROM conversations WHERE conversation_id = ?
            """, (conversation_id,))
            
            deleted = cursor.rowcount
            self._conn.commit()
            
            logger.info(f"Cleared conversation {conversation_id}, deleted {deleted} messages")
            return deleted
    
    def _truncate_content(self, content: str) -> str:
        """截断超长内容"""
        if len(content) <= self.max_chars_per_turn:
            return content
        return content[:self.max_chars_per_turn] + "..."
    
    def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None
