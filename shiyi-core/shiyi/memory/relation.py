"""RelationEngine - 关系引擎

功能：
- 关系存储（SQLite relations 表）：source_id, target_id, relation_type, weight
- 关系类型由 LLM 自动生成，不预设枚举
- 方法：add_relation, get_relations, get_related
- 双向关系支持
"""

import uuid
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from shiyi.common.types import Fragment
from shiyi.store.fragment_store import FragmentStore


class RelationEngine:
    """关系引擎 - 片段间关系网络"""
    
    def __init__(self, store: FragmentStore):
        """初始化关系引擎
        
        Args:
            store: FragmentStore 实例
        """
        self.store = store
    
    def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        weight: float = 1.0,
        bidirectional: bool = False,
    ) -> str:
        """添加关系
        
        Args:
            source_id: 源 Fragment ID
            target_id: 目标 Fragment ID
            relation_type: 关系类型（LLM自动生成）
            weight: 关系权重
            bidirectional: 是否双向添加
            
        Returns:
            关系 ID
        """
        rid = str(uuid.uuid4())
        
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO relations
                   (id, source_id, target_id, relation_type, weight, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rid, source_id, target_id, relation_type, weight, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        
        # 双向关系
        if bidirectional:
            self.add_relation(target_id, source_id, relation_type, weight, bidirectional=False)
        
        return rid
    
    def get_relations(
        self,
        fragment_id: str,
        relation_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取片段的所有关系
        
        Args:
            fragment_id: Fragment ID
            relation_type: 关系类型过滤，None 返回所有
            
        Returns:
            [{"id", "source_id", "target_id", "relation_type", "weight", "created_at"}, ...]
        """
        with sqlite3.connect(self.store.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            if relation_type:
                rows = conn.execute(
                    """SELECT * FROM relations
                       WHERE (source_id=? OR target_id=?) AND relation_type=?
                       ORDER BY created_at DESC""",
                    (fragment_id, fragment_id, relation_type)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM relations
                       WHERE source_id=? OR target_id=?
                       ORDER BY created_at DESC""",
                    (fragment_id, fragment_id)
                ).fetchall()
        
        return [dict(row) for row in rows]
    
    def get_related(
        self,
        fragment_id: str,
        relation_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[str]:
        """获取相关片段 ID 列表
        
        Args:
            fragment_id: Fragment ID
            relation_type: 关系类型过滤，None 返回所有
            limit: 返回数量上限
            
        Returns:
            [fragment_id, ...]
        """
        related_ids = set()
        
        relations = self.get_relations(fragment_id, relation_type)
        for rel in relations:
            if rel["source_id"] == fragment_id:
                related_ids.add(rel["target_id"])
            else:
                related_ids.add(rel["source_id"])
        
        return list(related_ids)[:limit]
    
    def get_neighbors(
        self,
        fragment_id: str,
        relation_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取邻居片段详情
        
        Args:
            fragment_id: Fragment ID
            relation_type: 关系类型过滤
            
        Returns:
            [{"fragment": Fragment, "relation": dict}, ...]
        """
        relations = self.get_relations(fragment_id, relation_type)
        
        neighbor_ids = []
        for rel in relations:
            nid = rel["target_id"] if rel["source_id"] == fragment_id else rel["source_id"]
            neighbor_ids.append(nid)
        
        if not neighbor_ids:
            return []
        
        fragments = self.store.batch_get(neighbor_ids)
        frag_map = {f.id: f for f in fragments}
        
        results = []
        for rel in relations:
            nid = rel["target_id"] if rel["source_id"] == fragment_id else rel["source_id"]
            f = frag_map.get(nid)
            if f:
                results.append({
                    "fragment": f,
                    "relation": rel,
                })
        
        return results
    
    def remove_relation(self, relation_id: str) -> bool:
        """删除关系
        
        Args:
            relation_id: 关系 ID
            
        Returns:
            是否删除成功
        """
        with sqlite3.connect(self.store.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM relations WHERE id=?",
                (relation_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def remove_relations_between(
        self,
        source_id: str,
        target_id: str,
        relation_type: Optional[str] = None,
    ) -> int:
        """删除两个片段间的所有关系
        
        Args:
            source_id: 源 Fragment ID
            target_id: 目标 Fragment ID
            relation_type: 关系类型过滤
            
        Returns:
            删除数量
        """
        with sqlite3.connect(self.store.db_path) as conn:
            if relation_type:
                cursor = conn.execute(
                    """DELETE FROM relations
                       WHERE ((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))
                       AND relation_type=?""",
                    (source_id, target_id, target_id, source_id, relation_type)
                )
            else:
                cursor = conn.execute(
                    """DELETE FROM relations
                       WHERE (source_id=? AND target_id=?) OR (source_id=? AND target_id=?)""",
                    (source_id, target_id, target_id, source_id)
                )
            conn.commit()
            return cursor.rowcount
    
    def get_relation_types(self) -> List[str]:
        """获取所有关系类型
        
        Returns:
            [relation_type, ...]
        """
        with sqlite3.connect(self.store.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT relation_type FROM relations ORDER BY relation_type"
            ).fetchall()
        return [row[0] for row in rows]
    
    def count_relations(self, fragment_id: Optional[str] = None) -> int:
        """统计关系数量
        
        Args:
            fragment_id: Fragment ID，None 则统计总数
            
        Returns:
            关系数量
        """
        with sqlite3.connect(self.store.db_path) as conn:
            if fragment_id:
                cursor = conn.execute(
                    """SELECT COUNT(*) FROM relations
                       WHERE source_id=? OR target_id=?""",
                    (fragment_id, fragment_id)
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM relations")
        return cursor.fetchone()[0]
    
    def get_relation_stats(self) -> Dict[str, Any]:
        """获取关系统计
        
        Returns:
            统计信息
        """
        with sqlite3.connect(self.store.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            
            by_type = conn.execute(
                "SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type ORDER BY COUNT(*) DESC"
            ).fetchall()
            
            # 计算平均度数
            cursor = conn.execute(
                """SELECT source_id FROM relations
                   UNION ALL
                   SELECT target_id FROM relations"""
            )
            degrees = {}
            for row in cursor:
                nid = row[0]
                degrees[nid] = degrees.get(nid, 0) + 1
            
            avg_degree = sum(degrees.values()) / max(len(degrees), 1)
        
        return {
            "total_relations": total,
            "unique_fragments": len(degrees),
            "by_type": {rt: cnt for rt, cnt in by_type},
            "avg_degree": round(avg_degree, 2),
        }
    
    # ═══════════════════════════════════════════
    # 自动关系提取（规则驱动）
    # ═══════════════════════════════════════════
    
    def extract_auto_relations(self, fragment: Fragment) -> List[Dict[str, Any]]:
        """自动提取关系（规则驱动）
        
        目前支持：
        - same_period: 时间窗口 ±3 天内
        - same_entity: 参与者重叠
        
        Args:
            fragment: 新 Fragment
            
        Returns:
            [{"source_id", "target_id", "relation_type", "weight"}, ...]
        """
        relations = []
        
        # 同一人（参与者重叠）
        same_entity = self._find_same_entity(fragment)
        relations.extend(same_entity)
        
        # 同时期（时间窗口 ±3 天）
        same_period = self._find_same_period(fragment, window_days=3)
        relations.extend(same_period)
        
        # 去重 + 写入
        seen = set()
        for rel in relations:
            key = (rel["target_id"], rel["relation_type"])
            if key not in seen:
                seen.add(key)
                self.add_relation(
                    fragment.id,
                    rel["target_id"],
                    rel["relation_type"],
                    rel["weight"],
                )
        
        return relations
    
    def _find_same_entity(self, fragment: Fragment) -> List[Dict[str, Any]]:
        """查找参与者重叠的关系"""
        if not fragment.scene_shell or not fragment.scene_shell.participants:
            return []
        
        results = []
        for p in fragment.scene_shell.participants:
            similar_ids = self.store.search_by_keyword(p, top_k=10)
            for sid in similar_ids:
                if sid and sid != fragment.id:
                    results.append({
                        "target_id": sid,
                        "relation_type": "same_entity",
                        "weight": 0.9,
                    })
        
        return results
    
    def _find_same_period(
        self,
        fragment: Fragment,
        window_days: int = 3,
    ) -> List[Dict[str, Any]]:
        """查找时间窗口内的关系"""
        if not fragment.time_shell or not fragment.time_shell.created_at:
            return []
        
        try:
            created = datetime.fromisoformat(fragment.time_shell.created_at)
        except (ValueError, TypeError):
            return []
        
        from datetime import timedelta
        start = (created - timedelta(days=window_days)).isoformat()
        end = (created + timedelta(days=window_days)).isoformat()
        
        frags = self.store.get_by_time_range(start, end, limit=50)
        
        return [
            {
                "target_id": f.id,
                "relation_type": "same_period",
                "weight": 0.6,
            }
            for f in frags if f.id and f.id != fragment.id
        ]
