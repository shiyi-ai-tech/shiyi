"""MemoryEngine - 记忆引擎门面

整合 FragmentStore、VectorIndex、DecayEngine、TriggerEngine、RelationEngine、CacheLayer

接口：
- recall(query, deep=False) -> List[Dict[str, Any]]
- remember(content: str) -> bool
- stats() -> Dict[str, Any]
"""

import uuid
import re
import logging
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

logger = logging.getLogger(__name__)


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
            except Exception as e:
                logger.warning("VectorIndex init failed (semantic search disabled): %s", e)
        
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
        # 空查询：直接返回最近的fragment，不走四路检索
        if not query or not query.strip():
            recent_frags = self.store.get_recent(limit=top_k)
            return [
                {"fragment": f, "score": 1.0, "source": "recent"}
                for f in recent_frags
            ]
        
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
        if self.vector_index and query_vector:
            try:
                vector_results = self.vector_index.search(query_vector, top_k=top_k * 2)
                for i, r in enumerate(vector_results):
                    if r["id"] not in results:
                        f = self.store.get(r["id"])
                        if f:
                            results[r["id"]] = {
                                "fragment": f,
                                "score": r["similarity"] * 0.9,
                                "source": "vector",
                            }
            except Exception as e:
                logger.warning(f"Vector search skipped: {e}")
        
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
            except Exception as e:
                logger.debug("Cold vector search skipped: %s", e)
        
        # 6. 四壳多维排序（v260530）
        self._sort_by_shells(results)

        # 7. 扩散检索（v260530）— 基于 embedding 的多跳扩散
        if self.vector_index and query_vector:
            self._diffuse(results, query_vector, max_depth=3, top_k=top_k)

        # 8. 扩散后重排序，避免扩散结果被截断
        self._sort_by_shells(results)

        return list(results.values())[:top_k]

    def _sort_by_shells(self, results: Dict[str, Dict[str, Any]]) -> None:
        """四壳多维排序 + 截断（v260530）

        对检索结果原地排序，权重：
        - 能量 0.4（当场算 decay_energy，不读库）
        - 情感 0.3（从 emotion_shell 取 valence 绝对值 + arousal）
        - 时间 0.2（从 time_shell 取新旧程度）
        - 场景 0.1（从 scene_shell 取匹配度）
        - 乘以原始召回分

        energy 当场调用 decay_engine.compute_energy() 计算，不依赖存储值。
        """
        import math
        now = datetime.now(timezone.utc)

        def shell_score(item: Dict[str, Any]) -> float:
            frag: Optional[Fragment] = item.get("fragment")
            if not frag:
                return item.get("score", 0.0)

            recall_score = item.get("score", 0.0)

            # 1. 能量（当场算）
            energy = self.decay_engine.compute_energy(frag)

            # 2. 情感：|valence| × 0.5 + arousal × 0.5
            es = frag.emotion_shell
            emotion = abs(getattr(es, 'valence', 0.0)) * 0.5 + getattr(es, 'arousal', 0.0) * 0.5
            emotion = min(1.0, max(0.0, emotion))

            # 3. 时间：越新越高
            ts = frag.time_shell
            time_score = 0.5  # 默认
            created_str = getattr(ts, 'created_at', '')
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    days_ago = (now - created).total_seconds() / 86400
                    # 指数衰减：半衰期 90 天
                    time_score = math.exp(-days_ago / 90.0 * math.log(2))
                except Exception:
                    pass

            # 4. 场景
            ss = frag.scene_shell
            scene = 0.5  # 默认
            scene_level = getattr(ss, 'activation_level', 0.0)
            if scene_level:
                scene = min(1.0, scene_level)

            # 加权组合
            final = recall_score * (
                0.4 * energy + 0.3 * emotion + 0.2 * time_score + 0.1 * scene
            )
            return final

        for fid in results:
            results[fid]["_shell_score"] = shell_score(results[fid])
            results[fid]["score"] = results[fid]["_shell_score"]

        # 原地排序
        sorted_items = sorted(
            results.items(),
            key=lambda kv: kv[1]["score"],
            reverse=True,
        )
        results.clear()
        for fid, item in sorted_items:
            results[fid] = item

    def _diffuse(
        self,
        results: Dict[str, Dict[str, Any]],
        query_vector: List[float],
        max_depth: int = 3,
        top_k: int = 10,
        link_threshold: float = 0.6,
    ) -> None:
        """扩散检索 — 多层 embedding 近邻扩散（v260530）

        从已召回结果出发，用原文关键词向量搜索近邻，
        多跳扩散直到梯度断崖或达最大深度。
        """
        import math

        if not self.vector_index:
            return

        candidates = list(results.values())[:5]
        visited = set(results.keys())
        depth = 0
        prev_avg_score = 1.0

        while depth < max_depth:
            new_additions = {}
            for cand in candidates:
                frag = cand.get("fragment")
                if not frag:
                    continue
                # 用 candidate fragment 自己的 embedding 做近邻搜索
                frag_id = getattr(frag, 'id', '')
                cand_vector = self.vector_index.get_vector(str(frag_id)) if frag_id else None
                search_vector = cand_vector if cand_vector else query_vector
                neighbors = self.vector_index.search(search_vector, top_k=top_k * 2)
                # 过滤掉已访问的
                for nb in neighbors:
                    nb_id = nb.get("id")
                    if not nb_id or nb_id in visited:
                        continue
                    visited.add(nb_id)
                    nb_frag = self.store.get(str(nb_id))
                    if not nb_frag:
                        continue
                    ls = self._link_score(cand, nb_frag, nb.get("similarity", 0.5))
                    if ls < link_threshold:
                        continue
                    new_additions[nb_id] = {
                        "fragment": nb_frag,
                        "score": ls,
                        "source": "diffusion",
                    }

            if not new_additions:
                break

            avg_score = sum(v["score"] for v in new_additions.values()) / len(new_additions)
            if depth > 0 and avg_score < prev_avg_score * 0.5:
                break
            prev_avg_score = avg_score

            for fid, item in new_additions.items():
                if fid not in results:
                    results[fid] = item

            # 按 link_score 降序选 top-5 作为下一跳种子
            candidates = sorted(new_additions.values(), key=lambda x: x["score"], reverse=True)[:5]
            depth += 1

    def _link_score(
        self,
        source: Dict[str, Any],
        target_frag: "Fragment",
        semantic_sim: float,
    ) -> float:
        """计算扩散链接分数

        link_score = semantic_similarity × time_decay × emotion_correlation

        各维度归一化到 [0,1]，三因子乘积。
        """
        import math
        now = datetime.now(timezone.utc)

        # 语义相似度
        sem = max(0.0, min(1.0, semantic_sim))

        # 时间衰减：目标 fragment 越新越相关
        time_decay = 1.0
        ts = target_frag.time_shell
        if ts and ts.created_at:
            try:
                created = datetime.fromisoformat(ts.created_at)
                days_ago = (now - created).total_seconds() / 86400
                time_decay = math.exp(-days_ago / 180.0 * math.log(2))
            except Exception:
                pass

        # 情感相关性
        source_frag = source.get("fragment")
        emo_corr = 0.5  # 默认中性
        if source_frag:
            s_emo = source_frag.emotion_shell
            t_emo = target_frag.emotion_shell
            # 同向：valence 乘积正 → 高相关
            s_val = getattr(s_emo, 'valence', 0.0)
            t_val = getattr(t_emo, 'valence', 0.0)
            # 相关性 = (1 - |s_val - t_val|/2) × arousal 匹配
            valence_match = 1.0 - min(1.0, abs(s_val - t_val) / 2.0)
            arousal_match = 1.0 - abs(
                getattr(s_emo, 'arousal', 0.0) - getattr(t_emo, 'arousal', 0.0)
            )
            emo_corr = (valence_match + arousal_match) / 2.0

        return round(sem * time_decay * emo_corr, 4)

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
                life_shell=LifeShell(confidence=1.0),  # energy 不存库，检索时当场算
                reply_context=reply_context,
                linked_to=linked_to,
                source_conversation_id=source_conversation_id,
            )
            
            # 统一去重（与 remember_fragment 一致）
            if embedding and self.vector_index and self.vector_index.count() > 0:
                try:
                    dedup_result = self._dedup_check(fragment, embedding)
                    if dedup_result == "duplicate":
                        return True
                except Exception:
                    pass  # 去重失败不影响正常存储
            
            self._store_fragment_internal(fragment, embedding)
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
            # energy 不存库，检索时用 decay_engine.compute_energy() 当场算

            # 统一去重 + 冲突检测
            if embedding and self.vector_index:
                try:
                    dedup_result = self._dedup_check(fragment, embedding)
                    if dedup_result == "duplicate":
                        return True
                except Exception:
                    pass  # 去重失败不影响存储

            self._store_fragment_internal(fragment, embedding)
            return True
        except Exception:
            return False

    def _store_fragment_internal(self, fragment, embedding=None) -> None:
        """内部统一存储：SQLite 插入 + 热层缓存 + 向量索引 + 关系提取"""
        self.store.insert(fragment)
        self.cache.put(fragment, layer="hot")
        if embedding and self.vector_index:
            self.vector_index.add_vector(fragment.id, embedding)
        self.relation_engine.extract_auto_relations(fragment)

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
                existing.life_shell.access_count += 1
                existing.time_shell.last_accessed_at = datetime.now(timezone.utc).isoformat()
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

    def save(self) -> None:
        """保存所有持久化数据"""
        if self.vector_index:
            self.vector_index.save()
    
    def close(self) -> None:
        """关闭引擎"""
        self.save()
        self.store.close()
