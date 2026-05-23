"""MemoryEngine - 记忆引擎门面

整合 FragmentStore、VectorIndex、DecayEngine、TriggerEngine、RelationEngine、CacheLayer

接口：
- recall(query, deep=False) -> List[Dict[str, Any]]
- remember(content: str) -> bool
- stats() -> Dict[str, Any]
"""

import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from shiyi.common.types import (
    Fragment, EmotionShell, SceneShell, TimeShell, LifeShell
)
from shiyi.store.fragment_store import FragmentStore
from shiyi.store.vector_index import VectorIndex
from shiyi.memory.decay import DecayEngine
from shiyi.memory.trigger import TriggerEngine
from shiyi.memory.relation import RelationEngine
from shiyi.memory.cache import CacheLayer


class MemoryEngine:
    """记忆引擎门面 - 整合所有记忆模块"""
    
    def __init__(
        self,
        db_path: str = "",
        index_path: str = "",
        halflife_days: float = 60.0,
        emotion_multiplier: float = 1.5,
        access_multiplier: float = 2.0,
        max_hops: int = 2,
        decay_per_hop: float = 0.5,
        hot_capacity: int = 50,
        embedding_dim: int = 1024,
    ):
        """初始化记忆引擎
        
        Args:
            db_path: SQLite 数据库路径
            index_path: 向量索引路径
            halflife_days: 衰减半衰期
            emotion_multiplier: 情感乘数
            access_multiplier: 访问乘数
            max_hops: 最大扩散跳数
            decay_per_hop: 每跳衰减
            hot_capacity: 热层容量
            embedding_dim: 向量维度
        """
        # 存储层
        self.store = FragmentStore(db_path=db_path)
        
        # 向量索引
        self.vector_index: Optional[VectorIndex] = None
        if embedding_dim > 0:
            try:
                self.vector_index = VectorIndex(
                    index_path=index_path,
                    dim=embedding_dim,
                )
            except Exception:
                pass
        
        # 缓存层
        self.cache = CacheLayer(
            store=self.store,
            vector_index=self.vector_index,
            hot_capacity=hot_capacity,
        )
        
        # 引擎
        self.decay_engine = DecayEngine(
            store=self.store,
            halflife_days=halflife_days,
            emotion_multiplier=emotion_multiplier,
            access_multiplier=access_multiplier,
        )
        
        self.trigger_engine = TriggerEngine(
            store=self.store,
            max_hops=max_hops,
            decay_per_hop=decay_per_hop,
        )
        
        self.relation_engine = RelationEngine(store=self.store)
    
    def recall(
        self,
        query: str,
        deep: bool = False,
        top_k: int = 10,
        query_vector: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """记忆检索 - 四路并行，融合截断
        
        检索流程：
        1. 关键词搜索（FTS5）
        2. 语义搜索（向量索引）
        3. 扩散搜索（BFS）
        4. 关系搜索
        
        Args:
            query: 查询文本
            deep: 是否深度检索（包含冷层）
            top_k: 返回数量
            
        Returns:
            [{"fragment": Fragment, "score": float, "source": str}, ...]
        """
        results: Dict[str, Dict[str, Any]] = {}
        
        # 1. FTS5 关键词搜索（基础权重 1.0）
        fts_ids = self.store.search_by_keyword(query, top_k=top_k * 2)
        for i, fid in enumerate(fts_ids):
            if fid not in results:
                f = self.store.get(fid)
                if f:
                    results[fid] = {
                        "fragment": f,
                        "score": 1.0 - (i / max(len(fts_ids), 1)) * 0.3,
                        "source": "fts5",
                    }
        
        # 2. 向量语义搜索（需要 query embedding，权重 0.9）
        # 这里简化处理，实际应该调用 embedding 模型
        # if self.vector_index and query_vector:
        #     vector_results = self.vector_index.search(query_vector, top_k=top_k * 2)
        #     for i, r in enumerate(vector_results):
        #         if r["id"] not in results:
        #             f = self.store.get(r["id"])
        #             if f:
        #                 results[r["id"]] = {
        #                     "fragment": f,
        #                     "score": r["similarity"] * 0.9,
        #                     "source": "vector",
        #                 }
        
        # 3. 扩散搜索（从 FTS 结果出发，权重 0.7）
        if fts_ids and self.trigger_engine.should_trigger(len(fts_ids)):
            diffused = self.trigger_engine.diffuse(fts_ids[:5], max_hops=self.trigger_engine.max_hops)
            for i, (fid, score) in enumerate(diffused):
                if fid not in results:
                    f = self.store.get(fid)
                    if f:
                        results[fid] = {
                            "fragment": f,
                            "score": score * 0.7,
                            "source": "trigger",
                        }
        
        # 4. 关系搜索（基础权重 0.5，仅做补充）
        for fid in list(results.keys())[:5]:
            related_ids = self.relation_engine.get_related(fid, limit=10)
            for rid in related_ids:
                if rid not in results:
                    f = self.store.get(rid)
                    if f:
                        results[rid] = {
                            "fragment": f,
                            "score": 0.5,
                            "source": "relation",
                        }
        
        # 5. 深度检索 - 冷层向量搜索（权重 0.6）
        if deep and self.vector_index and query_vector:
            try:
                cold_results = self.vector_index.search(query_vector, top_k=top_k * 2)
                for i, r in enumerate(cold_results):
                    if r["id"] not in results:
                        f = self.store.get(r["id"])
                        if f:
                            results[r["id"]] = {
                                "fragment": f,
                                "score": r["similarity"] * 0.6,
                                "source": "cold",
                            }
            except Exception:
                pass
        
        # 6. 融合增强：跨源互补权重 + 重排序
        # 出现在多路结果中的 Fragment 获得互补加分
        source_boost = {"fts5": 0.0, "vector": 0.0, "trigger": -0.05, "relation": -0.1, "cold": 0.0}
        
        for fid, result in results.items():
            base_score = result["score"]
            src = result.get("source", "fts5")
            boost = source_boost.get(src, 0.0)
            
            # 主检索路（fts5/vector）额外 +0.05
            if src in ("fts5", "vector"):
                boost += 0.05
            
            result["score"] = min(1.0, max(0.0, base_score + boost))
        
        # 排序并截断
        sorted_results = sorted(
            results.values(),
            key=lambda x: x["score"],
            reverse=True,
        )
        
        return sorted_results[:top_k]
    
    def remember(
        self,
        content: str,
        emotion: Optional[EmotionShell] = None,
        scene: Optional[SceneShell] = None,
        source_conversation_id: str = "",
        reply_context: str = "",
        linked_to: str = "",
        embedding: Optional[List[float]] = None,
    ) -> bool:
        """记忆存储 - 创建新 Fragment
        
        Args:
            content: 记忆内容（fact_kernel）
            emotion: 情感壳
            scene: 场景壳
            source_conversation_id: 来源对话 ID
            reply_context: AI 回复中与此事实对应的部分
            linked_to: 关联的 Fragment ID
            embedding: 向量（可选）
            
        Returns:
            是否存储成功
        """
        # 拒绝空内容
        if not content or not content.strip():
            return False
        
        # 拒绝无有效字符的输入（纯标点/符号/空白）
        import re
        if not re.search(r'[\w\u4e00-\u9fff\u3400-\u4dbf]', content):
            return False
        
        try:
            # 创建 Fragment
            fragment = Fragment(
                id=str(uuid.uuid4()),
                fact_kernel=content,
                emotion_shell=emotion or EmotionShell(),
                scene_shell=scene or SceneShell(),
                time_shell=TimeShell(created_at=datetime.now(timezone.utc).isoformat()),
                life_shell=LifeShell(energy=1.0, confidence=1.0),
                reply_context=reply_context,
                linked_to=linked_to,
                source_conversation_id=source_conversation_id,
            )
            
            # 计算衰减能量
            energy = self.decay_engine.compute_energy(fragment)
            fragment.life_shell.energy = energy
            
            # 存储到 SQLite
            self.store.insert(fragment)
            
            # 添加到热层缓存
            self.cache.put(fragment, layer="hot")
            
            # 添加到向量索引
            if embedding and self.vector_index:
                self.vector_index.add_vector(fragment.id, embedding)
            
            # 自动提取关系
            self.relation_engine.extract_auto_relations(fragment)
            
            return True
        
        except Exception as e:
            return False
    
    def remember_fragment(
        self,
        fragment,
        embedding=None,
    ) -> bool:
        """直接存储 Fragment 对象"""
        if not fragment or not fragment.fact_kernel or not fragment.fact_kernel.strip():
            return False
        try:
            energy = self.decay_engine.compute_energy(fragment)
            fragment.life_shell.energy = energy

            # ═══ 深度整理：去重 + 冲突检测 ═══
            # 在插入前用 embedding 做语义相似度比对，复用现有向量索引
            if embedding and self.vector_index:
                try:
                    dedup_result = self._dedup_check(fragment, embedding)
                    if dedup_result == "duplicate":
                        return True  # 已存在相同记忆，跳过插入
                    # "conflict" / "new" → 继续插入
                except Exception:
                    pass  # 去重失败不影响存储

            self.store.insert(fragment)
            self.cache.put(fragment, layer="hot")
            if embedding and self.vector_index:
                self.vector_index.add_vector(fragment.id, embedding)
            self.relation_engine.extract_auto_relations(fragment)
            return True
        except Exception:
            return False

    def _dedup_check(
        self,
        fragment: 'Fragment',
        embedding: list,
        threshold: float = 0.15,  # 余弦距离 < 0.15 → 相似度 > 0.85
    ) -> str:
        """语义去重 + 冲突检测

        在插入新 Fragment 前，用向量搜索已有记忆：
        - 高相似 + 同向情感 → "duplicate"（跳过插入，刷新旧记忆）
        - 高相似 + 反向情感 → "conflict"（标记冲突关系，两存）
        - 无高相似 → "new"（正常插入）

        Returns:
            "new" / "duplicate" / "conflict"
        """
        if not self.vector_index:
            return "new"

        # 搜索 top 3 最相似片段
        hits = self.vector_index.search(embedding, top_k=3)
        if not hits:
            return "new"

        for hit in hits:
            if hit["distance"] > threshold:
                continue  # 相似度不够

            existing = self.store.get(hit["id"])
            if not existing:
                continue

            new_valence = fragment.emotion_shell.valence if fragment.emotion_shell else 0.0
            old_valence = existing.emotion_shell.valence if existing.emotion_shell else 0.0

            # 判定情感方向：同向 vs 反向
            same_sign = (new_valence >= -0.1 and old_valence >= -0.1) or \
                        (new_valence <= 0.1 and old_valence <= 0.1)

            if same_sign:
                # 去重：刷新旧记忆（访问次数+1，更新最后访问时间）
                from datetime import datetime, timezone as tz
                existing.life_shell.access_count += 1
                existing.time_shell.last_accessed_at = datetime.now(tz.utc).isoformat()
                # 旧记忆被重新提及，能量回升
                existing.life_shell.energy = self.decay_engine.compute_energy(existing)
                self.store.update(existing)
                self.cache.put(existing, layer="hot")
                return "duplicate"

            # 冲突：情感反向 → 标记双向冲突关系
            self.relation_engine.add_relation(
                fragment.id,
                existing.id,
                "conflicts_with",
                weight=0.9,
                bidirectional=True,
            )

        return "new"
    
    def refresh(self, fragment_id: str) -> bool:
        """刷新 Fragment - 被检索命中时调用
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            是否成功
        """
        fragment = self.store.get(fragment_id)
        if not fragment:
            return False
        
        # 衰减引擎刷新
        self.decay_engine.refresh(fragment)
        
        # 更新存储
        self.store.update(fragment)
        
        # 添加到热层
        self.cache.put(fragment, layer="hot")
        
        return True
    
    def stats(self) -> Dict[str, Any]:
        """统计信息
        
        Returns:
            统计信息字典
        """
        cache_stats = self.cache.stats()
        relation_stats = self.relation_engine.get_relation_stats()
        
        return {
            "fragments": {
                "total": self.store.count(),
                "by_layer": cache_stats.get("warm", {}).get("layer_counts", {}),
            },
            "by_layer": cache_stats.get("warm", {}).get("layer_counts", {}),
            "cache": cache_stats,
            "relations": relation_stats,
            "vector_index": {
                "count": self.vector_index.count() if self.vector_index else 0,
            },
        }
    
    def decay_all(self) -> int:
        """执行批量衰减
        
        Returns:
            处理的 Fragment 数量
        """
        count = self.decay_engine.decay_all()
        # 衰减后清理热层缓存（能量可能已变）
        self.cache.clear_hot()
        return count
    
    def save(self) -> None:
        """保存所有持久化数据"""
        if self.vector_index:
            self.vector_index.save()
    
    def close(self) -> None:
        """关闭引擎"""
        self.save()
