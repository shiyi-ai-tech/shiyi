"""Phase 3 决策层单元测试

测试各模块的功能：
1. PromptAssembler - 组装messages包含对话历史+检索结果
2. FragmentExtractor - 提取json中的Fragment，过滤不独立的
3. VectorSearch - 无EmbeddingProvider时优雅降级
4. DecideEngine - 完整链路（降级模式）
5. talk() - 调用完整链路返回回复
"""

import sys
sys.path.insert(0, 'shiyi-common')
sys.path.insert(0, 'shiyi-core')
sys.path.insert(0, 'shiyi-shell')

import unittest
from unittest.mock import Mock, MagicMock


class TestPromptAssembler(unittest.TestCase):
    """测试 PromptAssembler"""
    
    def test_assemble_with_history(self):
        """测试：组装messages包含对话历史+检索结果"""
        from shiyi.decision.prompt_assembler import PromptAssembler
        
        assembler = PromptAssembler()
        
        # Mock数据
        intent_result = Mock()
        intent_result.intent = "chat"
        intent_result.sub_queries = []
        
        fragments = [
            {"fact_kernel": "用户叫小明", "score": 0.9, "emotion_shell": {"primary": "开心"}},
        ]
        
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，我是史佚"},
        ]
        
        messages = assembler.assemble(
            intent_result=intent_result,
            fragments=fragments,
            conversation_history=history,
        )
        
        # 验证：应该有system和user两个消息
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        
        # 验证：用户消息包含对话历史
        user_content = messages[1]["content"]
        self.assertIn("你好", user_content)  # 历史对话
        self.assertIn("用户叫小明", user_content)  # 检索结果
    
    def test_assemble_without_history(self):
        """测试：无对话历史时正常组装"""
        from shiyi.decision.prompt_assembler import PromptAssembler
        
        assembler = PromptAssembler()
        
        intent_result = Mock()
        intent_result.intent = "query"
        
        messages = assembler.assemble(
            intent_result=intent_result,
            fragments=[],
            conversation_history=[],
        )
        
        self.assertEqual(len(messages), 2)
        # 无历史时不显示历史部分，只显示相关记忆和当前输入


class TestFragmentExtractor(unittest.TestCase):
    """测试 FragmentExtractor"""
    
    def test_extract_from_json_block(self):
        """测试：从```json```块中提取Fragment"""
        from shiyi.decision.fragment_extractor import FragmentExtractor
        
        extractor = FragmentExtractor()
        
        reply = '''你好！我来回复你。
        
```json
[
  {
    "fact_kernel": "用户叫小明",
    "emotion_shell": {"valence": 0.5, "arousal": 0.3, "primary": "开心"},
    "linked_to": ""
  }
]
```'''
        
        fragments = extractor.extract(reply)
        
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0]["fact_kernel"], "用户叫小明")
    
    def test_filter_empty_fact_kernel(self):
        """测试：过滤空fact_kernel"""
        from shiyi.decision.fragment_extractor import FragmentExtractor
        
        extractor = FragmentExtractor()
        
        reply = '''
```json
[
  {"fact_kernel": "", "emotion_shell": {}},
  {"fact_kernel": "有效的事实", "emotion_shell": {}}
]
```'''
        
        fragments = extractor.extract(reply)
        
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0]["fact_kernel"], "有效的事实")
    
    def test_extract_reply_only(self):
        """测试：提取仅回复部分"""
        from shiyi.decision.fragment_extractor import FragmentExtractor
        
        extractor = FragmentExtractor()
        
        reply = '''你好！我很高兴认识你。
        
```json
[
  {"fact_kernel": "用户叫小明", "emotion_shell": {}}
]
```'''
        
        reply_only = extractor.extract_reply_only(reply)
        
        self.assertNotIn("```", reply_only)
        self.assertIn("你好", reply_only)
        self.assertIn("很高兴认识你", reply_only)
    
    def test_empty_reply(self):
        """测试：空回复返回空列表"""
        from shiyi.decision.fragment_extractor import FragmentExtractor
        
        extractor = FragmentExtractor()
        
        fragments = extractor.extract("")
        self.assertEqual(len(fragments), 0)


class TestVectorSearch(unittest.TestCase):
    """测试 VectorSearch"""
    
    def test_no_embedding_provider(self):
        """测试：无EmbeddingProvider时优雅降级"""
        from shiyi.decision.vector_search import VectorSearch
        
        search = VectorSearch(embedding_provider=None, vector_index=None)
        
        # 验证：is_available应为False
        self.assertFalse(search.is_available)
        
        # 验证：search应返回空列表
        results = search.search("测试查询", top_k=5)
        self.assertEqual(len(results), 0)
    
    def test_mock_embedding_provider(self):
        """测试：带Mock EmbeddingProvider"""
        from shiyi.decision.vector_search import VectorSearch
        
        mock_provider = Mock()
        mock_provider.is_available.return_value = True
        mock_provider.embed.return_value = [0.1, 0.2, 0.3]
        mock_provider.dimension = 3
        
        mock_index = Mock()
        mock_index.search.return_value = []
        
        search = VectorSearch(
            embedding_provider=mock_provider,
            vector_index=mock_index,
        )
        
        self.assertTrue(search.is_available)


class TestDecideEngine(unittest.TestCase):
    """测试 DecideEngine"""
    
    def test_decide_without_llm(self):
        """测试：无LLM时的降级决策"""
        from shiyi.decision.decide_engine import DecideEngine
        from shiyi.memory.engine import MemoryEngine
        from shiyi.perception.intent_engine import IntentEngine
        from shiyi.perception.conversation import ConversationManager
        
        # 创建组件（无LLM）
        memory = MemoryEngine()
        intent_engine = IntentEngine(llm_provider=None)
        conversation = ConversationManager()
        
        engine = DecideEngine(
            memory_engine=memory,
            intent_engine=intent_engine,
            conversation_manager=conversation,
            llm_provider=None,
        )
        
        # 验证：llm_available应为False
        self.assertFalse(engine.llm_available)
        
        # 执行决策
        result = engine.decide(
            query="你好",
            session_id="test",
        )
        
        # 验证：应该有回复
        self.assertIsNotNone(result.reply)
        self.assertFalse(result.llm_used)


class TestShiyiTalk(unittest.TestCase):
    """测试 Shiyi.talk()"""
    
    def test_talk_fallback_mode(self):
        """测试：降级模式下的talk()"""
        from shiyi.engine import Shiyi
        
        shiyi = Shiyi(llm_provider=None, embedding_provider=None)
        
        # 验证：llm_available应为False
        self.assertFalse(shiyi.llm_available)
        
        # 执行talk
        reply = shiyi.talk("你好")
        
        # 验证：应该有回复
        self.assertIsNotNone(reply)
        self.assertIsInstance(reply, str)
    
    def test_talk_with_content(self):
        """测试：talk()处理用户内容"""
        from shiyi.engine import Shiyi
        
        shiyi = Shiyi(llm_provider=None, embedding_provider=None)
        
        reply = shiyi.talk("我是小明，我喜欢编程")
        
        # 验证：应该有回复
        self.assertIsNotNone(reply)


if __name__ == '__main__':
    # 减少jieba日志
    import logging
    logging.getLogger('jieba').setLevel(logging.WARNING)
    
    unittest.main(verbosity=2)
