"""Tests for translate_polymer.parse_paper_note."""
import translate_polymer as tp

SAMPLE = '''---
arxiv_id: "2401.12345"
title: "Cool Polymer Dynamics"
---

# Cool Polymer Dynamics

## Abstract

We study polymer dynamics and entanglement.

---

## Reading Notes

## 一句话总结
这是一篇关于高分子动力学的研究论文，作者提出了新的模型来描述链段运动与缠结效应，并在多种条件下进行了验证。

## 核心方法
- 采用基于高斯的原子几何描述子生成等变特征。
- 利用图神经网络自适应学习原子化学环境。
'''


def test_parse_title():
    parsed = tp.parse_paper_note(SAMPLE)
    assert parsed["title_en"] == "Cool Polymer Dynamics"


def test_parse_abstract():
    parsed = tp.parse_paper_note(SAMPLE)
    assert parsed["abstract_en"] == "We study polymer dynamics and entanglement."


def test_notes_already_zh_detected():
    parsed = tp.parse_paper_note(SAMPLE)
    assert parsed.get("notes_already_zh") is True


def test_translate_cache_roundtrip():
    cache = {}
    assert tp.tr(cache, "hello") == "hello"  # miss returns original
    cache[tp._key("hello")] = "你好"
    assert tp.tr(cache, "hello") == "你好"
