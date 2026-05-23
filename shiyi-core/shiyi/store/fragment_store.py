"""FragmentStore - SQLite + FTS5 存储层

功能：
- fragments 表存储完整 Fragment 数据（JSON序列化）
- FTS5 虚拟表提供全文检索
- CRUD 操作：insert, get, update, batch_get, delete
- FTS5 关键词搜索：search_by_keyword
- 统计：count, get_by_time_range
"""

import json
import uuid
import sqlite3
import jieba
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from shiyi.common.types import (
    Fragment, EmotionShell, SceneShell, TimeShell, LifeShell
)
from shiyi.common.errors import StorageError
from shiyi.common.utils import energy_to_layer


class FragmentStore:
    """Fragment SQLite 持久化存储 + FTS5 全文检索"""
    
    def __init__(self, db_path: str = ""):
        """初始化存储
        
        Args:
            db_path: 数据库路径，默认 ~/.shiyi/data/fragments.db
        """
        if not db_path:
            db_path = str(Path.home() / ".shiyi" / "data" / "fragments.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self) -> None:
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            # 尝试添加 layer 列（从旧版本迁移）
            try:
                conn.execute("ALTER TABLE fragments ADD COLUMN layer TEXT DEFAULT 'warm'")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # 列已存在
            
            # fragments 主表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fragments (
                    id TEXT PRIMARY KEY,
                    fact_kernel TEXT NOT NULL,
                    emotion_shell TEXT DEFAULT '{}',
                    scene_shell TEXT DEFAULT '{}',
                    time_shell TEXT DEFAULT '{}',
                    life_shell TEXT DEFAULT '{}',
                    reply_context TEXT DEFAULT '',
                    linked_to TEXT DEFAULT '',
                    source_conversation_id TEXT DEFAULT '',
                    embedding_model TEXT DEFAULT '',
                    embedding_version TEXT DEFAULT '',
                    layer TEXT DEFAULT 'warm',
                    created_at TEXT DEFAULT ''
                )
            """)
            
            # FTS5 虚拟表
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS fragments_fts USING fts5(
                    fact_kernel,
                    content='fragments',
                    content_rowid='rowid'
                )
            """)
            
            # 触发器：自动同步 FTS
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS fragments_ai AFTER INSERT ON fragments BEGIN
                    INSERT INTO fragments_fts(rowid, fact_kernel) VALUES (NEW.rowid, NEW.fact_kernel);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS fragments_ad AFTER DELETE ON fragments BEGIN
                    INSERT INTO fragments_fts(fragments_fts, rowid, fact_kernel) VALUES('delete', OLD.rowid, OLD.fact_kernel);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS fragments_au AFTER UPDATE ON fragments BEGIN
                    INSERT INTO fragments_fts(fragments_fts, rowid, fact_kernel) VALUES('delete', OLD.rowid, OLD.fact_kernel);
                    INSERT INTO fragments_fts(rowid, fact_kernel) VALUES (NEW.rowid, NEW.fact_kernel);
                END
            """)
            
            # relations 表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS relations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    created_at TEXT DEFAULT ''
                )
            """)
            
            # 索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_layer ON fragments(layer)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON relations(source_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_target ON relations(target_id)")
            
            conn.commit()
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    # ═══════════════════════════════════════════
    # CRUD 操作
    # ═══════════════════════════════════════════
    
    def insert(self, fragment: Fragment) -> str:
        """插入 Fragment
        
        Args:
            fragment: Fragment 实例
            
        Returns:
            fragment.id
        """
        if not fragment.id:
            fragment.id = str(uuid.uuid4())
        if not fragment.time_shell.created_at:
            fragment.time_shell.created_at = datetime.now(timezone.utc).isoformat()
        if not fragment.life_shell:
            fragment.life_shell = LifeShell()
        
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO fragments 
                   (id, fact_kernel, emotion_shell, scene_shell, time_shell,
                    life_shell, reply_context, linked_to, source_conversation_id,
                    embedding_model, embedding_version, layer, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fragment.id,
                    fragment.fact_kernel,
                    json.dumps(self._emotion_to_dict(fragment.emotion_shell)),
                    json.dumps(self._scene_to_dict(fragment.scene_shell)),
                    json.dumps(self._time_to_dict(fragment.time_shell)),
                    json.dumps(self._life_to_dict(fragment.life_shell, fragment.linked_to)),
                    fragment.reply_context or "",
                    fragment.linked_to or "",
                    fragment.source_conversation_id or "",
                    fragment.embedding_model or "",
                    fragment.embedding_version or "",
                    energy_to_layer(fragment.life_shell.energy if fragment.life_shell else 1.0),
                    fragment.time_shell.created_at,
                )
            )
            conn.commit()
        
        return fragment.id
    
    def get(self, fragment_id: str) -> Optional[Fragment]:
        """根据 ID 获取 Fragment
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            Fragment 实例或 None
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM fragments WHERE id=?",
                (fragment_id,)
            ).fetchone()
        
        if not row:
            return None
        return self._row_to_fragment(row)
    
    def update(self, fragment: Fragment) -> None:
        """更新 Fragment
        
        Args:
            fragment: Fragment 实例
        """
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE fragments SET
                   fact_kernel=?, emotion_shell=?, scene_shell=?, time_shell=?,
                   life_shell=?, reply_context=?, linked_to=?, layer=?
                   WHERE id=?""",
                (
                    fragment.fact_kernel,
                    json.dumps(self._emotion_to_dict(fragment.emotion_shell)),
                    json.dumps(self._scene_to_dict(fragment.scene_shell)),
                    json.dumps(self._time_to_dict(fragment.time_shell)),
                    json.dumps(self._life_to_dict(fragment.life_shell, fragment.linked_to)),
                    fragment.reply_context or "",
                    fragment.linked_to or "",
                    energy_to_layer(fragment.life_shell.energy if fragment.life_shell else 1.0),
                    fragment.id,
                )
            )
            conn.commit()
    
    def batch_get(self, fragment_ids: List[str]) -> List[Fragment]:
        """批量获取 Fragment
        
        Args:
            fragment_ids: Fragment ID 列表
            
        Returns:
            Fragment 列表
        """
        if not fragment_ids:
            return []
        placeholders = ",".join("?" * len(fragment_ids))
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM fragments WHERE id IN ({placeholders})",
                fragment_ids
            ).fetchall()
        return [self._row_to_fragment(row) for row in rows]
    
    def delete(self, fragment_id: str) -> bool:
        """删除 Fragment
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            是否删除成功
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM fragments WHERE id=?",
                (fragment_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    # ═══════════════════════════════════════════
    # FTS5 搜索
    # ═══════════════════════════════════════════
    
    def search_by_keyword(self, query: str, top_k: int = 20) -> List[str]:
        """FTS5 全文搜索
        
        Args:
            query: 搜索关键词
            top_k: 返回数量
            
        Returns:
            Fragment ID 列表
        """
        if not query or not query.strip():
            return []
        
        # 中文停用词
        _CHINESE_STOPWORDS = frozenset({
            '的', '了', '和', '是', '在', '有', '我', '你', '他', '她', '它',
            '这', '那', '个', '一', '不', '就', '也', '都', '而', '及', '与',
            '着', '或', '一个', '我们', '你们', '他们', '她们', '它们',
            '着', '被', '把', '让', '给', '向', '到', '从', '为', '以',
        })
        
        # 使用 jieba 分词（支持中英文）
        terms = list(jieba.cut_for_search(query.strip()))
        # 过滤空词和停用词
        terms = [t for t in terms if t.strip() and t not in _CHINESE_STOPWORDS]
        if not terms:
            return []
        
        with self._get_conn() as conn:
            # 检测是否包含中文（需要 LIKE 模糊匹配）
            has_chinese = any('\u4e00' <= c <= '\u9fff' for c in query)
            
            if has_chinese:
                # 中文搜索：LIKE 多词匹配
                # 策略：AND 先搜 → 0结果时 OR 回退 → OR结果按命中词数排序
                and_conditions = " AND ".join(["fact_kernel LIKE ?" for _ in terms])
                and_params = [f"%{t}%" for t in terms] + [top_k]
                rows = conn.execute(
                    f"SELECT id FROM fragments WHERE {and_conditions} LIMIT ?",
                    and_params
                ).fetchall()
                
                if not rows:
                    # AND 无结果，OR 回退：任一词匹配即可
                    or_conditions = " OR ".join(["fact_kernel LIKE ?" for _ in terms])
                    or_params = [f"%{t}%" for t in terms]
                    or_rows = conn.execute(
                        f"SELECT id FROM fragments WHERE {or_conditions}",
                        or_params
                    ).fetchall()
                    
                    # 按命中词数排序（命中越多排越前）
                    if or_rows:
                        scored = []
                        for row in or_rows:
                            frag = self.get(row["id"])
                            if frag:
                                hit_count = sum(1 for t in terms if t in frag.fact_kernel)
                                scored.append((row["id"], hit_count))
                        scored.sort(key=lambda x: x[1], reverse=True)
                        rows = [{"id": sid} for sid, _ in scored[:top_k]]
            else:
                # 英文/数字搜索：使用 FTS5
                try:
                    fts_conditions = " OR ".join(["fact_kernel MATCH ?" for _ in terms])
                    rows = conn.execute(
                        f"""SELECT f.id FROM fragments f
                           JOIN fragments_fts fts ON f.rowid = fts.rowid
                           WHERE {fts_conditions}
                           LIMIT ?""",
                        [f"*{t}*" for t in terms] + [top_k]
                    ).fetchall()
                except sqlite3.OperationalError:
                    # FTS5 查询语法错误时使用 LIKE 回退
                    like_pattern = f"%{'%'.join(terms)}%"
                    rows = conn.execute(
                        "SELECT id FROM fragments WHERE fact_kernel LIKE ? LIMIT ?",
                        (like_pattern, top_k)
                    ).fetchall()
            
            return [row["id"] for row in rows]
    
    # ═══════════════════════════════════════════
    # 统计查询
    # ═══════════════════════════════════════════
    
    def count(self) -> int:
        """Fragment 总数
        
        Returns:
            总数
        """
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM fragments").fetchone()
        return row[0] if row else 0
    
    def count_by_layer(self) -> Dict[str, int]:
        """按层级统计数量
        
        Returns:
            {layer: count}
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT layer, COUNT(*) as cnt FROM fragments GROUP BY layer"
            ).fetchall()
        return {row["layer"]: row["cnt"] for row in rows}
    
    def get_by_layer(self, layer: str, limit: int = 500) -> List[Fragment]:
        """获取指定层级的 Fragment
        
        Args:
            layer: warm / hot / cold
            limit: 返回数量上限
            
        Returns:
            Fragment 列表
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM fragments 
                   WHERE layer=? 
                   ORDER BY created_at DESC 
                   LIMIT ?""",
                (layer, limit)
            ).fetchall()
        return [self._row_to_fragment(row) for row in rows]
    
    def get_by_time_range(
        self, 
        start: Optional[str] = None, 
        end: Optional[str] = None,
        limit: int = 100
    ) -> List[Fragment]:
        """按时间范围查询
        
        Args:
            start: 开始时间 ISO 格式
            end: 结束时间 ISO 格式
            limit: 返回数量上限
            
        Returns:
            Fragment 列表
        """
        query = "SELECT * FROM fragments WHERE 1=1"
        params = []
        
        if start:
            query += " AND created_at >= ?"
            params.append(start)
        if end:
            query += " AND created_at <= ?"
            params.append(end)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_fragment(row) for row in rows]
    
    def get_recent(self, limit: int = 100) -> List[Fragment]:
        """获取最近的 Fragment
        
        Args:
            limit: 返回数量上限
            
        Returns:
            Fragment 列表
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM fragments ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [self._row_to_fragment(row) for row in rows]
    
    # ═══════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════
    
    def _row_to_fragment(self, row: sqlite3.Row) -> Fragment:
        """数据库行转换为 Fragment"""
        try:
            es = json.loads(row["emotion_shell"])
            emotion_shell = EmotionShell(
                valence=es.get("valence", 0.0),
                arousal=es.get("arousal", 0.0),
                primary=es.get("primary", "中性"),
            )
        except (json.JSONDecodeError, TypeError):
            emotion_shell = EmotionShell()
        
        try:
            ss = json.loads(row["scene_shell"])
            scene_shell = SceneShell(
                domain=ss.get("domain", ""),
                participants=ss.get("participants", []),
                location=ss.get("location", ""),
            )
        except (json.JSONDecodeError, TypeError):
            scene_shell = SceneShell()
        
        try:
            ts = json.loads(row["time_shell"])
            time_shell = TimeShell(
                created_at=ts.get("created_at", ""),
                referenced_time=ts.get("referenced_time", ""),
            )
        except (json.JSONDecodeError, TypeError):
            time_shell = TimeShell()
        
        try:
            ls = json.loads(row["life_shell"])
            life_shell = LifeShell(
                energy=ls.get("energy", 1.0),
                confidence=ls.get("confidence", 1.0),
                access_count=ls.get("access_count", 0),
                forgotten=ls.get("forgotten", False),
            )
        except (json.JSONDecodeError, TypeError):
            life_shell = LifeShell()
        
        return Fragment(
            id=row["id"],
            fact_kernel=row["fact_kernel"],
            emotion_shell=emotion_shell,
            scene_shell=scene_shell,
            time_shell=time_shell,
            life_shell=life_shell,
            reply_context=row["reply_context"],
            linked_to=row["linked_to"],
            source_conversation_id=row["source_conversation_id"],
            embedding_model=row["embedding_model"],
            embedding_version=row["embedding_version"],
        )
    
    def _emotion_to_dict(self, emotion: EmotionShell) -> Dict[str, Any]:
        return {
            "valence": emotion.valence,
            "arousal": emotion.arousal,
            "primary": emotion.primary,
        }
    
    def _scene_to_dict(self, scene: SceneShell) -> Dict[str, Any]:
        return {
            "domain": scene.domain,
            "participants": scene.participants,
            "location": scene.location,
        }
    
    def _time_to_dict(self, time_shell: TimeShell) -> Dict[str, Any]:
        return {
            "created_at": time_shell.created_at,
            "referenced_time": time_shell.referenced_time,
        }
    
    def _life_to_dict(self, life: LifeShell, linked_to: str = "") -> Dict[str, Any]:
        return {
            "energy": life.energy,
            "confidence": life.confidence,
            "access_count": life.access_count,
            "forgotten": life.forgotten,
            "linked_to": linked_to,
        }
