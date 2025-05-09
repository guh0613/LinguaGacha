import re

from base.Base import Base
from base.BaseLanguage import BaseLanguage
from module.Text.TextHelper import TextHelper
from module.Cache.CacheItem import CacheItem
from module.Config import Config
from module.Filter.RuleFilter import RuleFilter
from module.Filter.LanguageFilter import LanguageFilter
from module.TextPreserver import TextPreserver

class ResponseChecker(Base):

    class Error():

        NONE: str = "NONE"
        UNKNOWN: str = "UNKNOWN"
        FAIL_DATA: str = "FAIL_DATA"
        FAIL_LINE_COUNT: str = "FAIL_LINE_COUNT"
        LINE_ERROR_KANA: str = "LINE_ERROR_KANA"
        LINE_ERROR_HANGEUL: str = "LINE_ERROR_HANGEUL"
        LINE_ERROR_FAKE_REPLY: str = "LINE_ERROR_FAKE_REPLY"
        LINE_ERROR_EMPTY_LINE: str = "LINE_ERROR_EMPTY_LINE"
        LINE_ERROR_SIMILARITY: str = "LINE_ERROR_SIMILARITY"
        LINE_ERROR_DEGRADATION: str = "LINE_ERROR_DEGRADATION"

        LINE_ERROR: tuple[str] = (
            LINE_ERROR_KANA,
            LINE_ERROR_HANGEUL,
            LINE_ERROR_FAKE_REPLY,
            LINE_ERROR_EMPTY_LINE,
            LINE_ERROR_SIMILARITY,
            LINE_ERROR_DEGRADATION,
        )

    # 重试次数阈值
    RETRY_COUNT_THRESHOLD: int = 2

    # 退化检测规则
    RE_DEGRADATION = re.compile(r"(.{1,2})\1{16,}", flags = re.IGNORECASE)

    def __init__(self, config: Config, items: list[CacheItem]) -> None:
        super().__init__()

        # 初始化
        self.items = items
        self.config = config
        self.source_language = self.config.source_language
        self.target_language = self.config.target_language

    # 检查
    def check(self, src_dict: dict[str, str], dst_dict: dict[str, str], item_dict: dict[str, CacheItem], source_language: BaseLanguage.Enum) -> list[str]:
        # 数据解析失败
        if len(dst_dict) == 0 or all(v == "" or v == None for v in dst_dict.values()):
            return [ResponseChecker.Error.FAIL_DATA] * len(src_dict)

        # 当翻译任务为单条目任务，且此条目已经是第二次单独重试时，直接返回，不进行后续判断
        if len(self.items) == 1 and self.items[0].get_retry_count() >= ResponseChecker.RETRY_COUNT_THRESHOLD:
            return [ResponseChecker.Error.NONE] * len(src_dict)

        # 行数检查
        if len(src_dict) != len(dst_dict):
            return [ResponseChecker.Error.FAIL_LINE_COUNT] * len(src_dict)

        # 逐行检查
        error = self.check_lines(src_dict, dst_dict, item_dict, source_language)
        if any(v != ResponseChecker.Error.NONE for v in error):
            return error

        # 默认无错误
        return [ResponseChecker.Error.NONE] * len(src_dict)

    # 逐行检查错误
    def check_lines(self, src_dict: dict[str, str], dst_dict: dict[str, str], item_dict: dict[str, CacheItem], source_language: BaseLanguage.Enum) -> list[str]:
        check_result: list[int] = []
        for src, dst, item in zip(src_dict.values(), dst_dict.values(), item_dict.values()):
            src = src.strip()
            dst = dst.strip()

            # 原文不为空而译文为空时，判断为错误翻译
            if src != "" and dst == "":
                check_result.append(ResponseChecker.Error.LINE_ERROR_EMPTY_LINE)
                continue

            # 原文内容包含代码救星占位符时，判断为正确翻译
            if TextPreserver.PLACEHOLDER in src:
                check_result.append(ResponseChecker.Error.NONE)
                continue

            # 原文内容符合规则过滤条件时，判断为正确翻译
            if RuleFilter.filter(src, item.get_skip_internal_filter()) == True:
                check_result.append(ResponseChecker.Error.NONE)
                continue

            # 原文内容符合语言过滤条件时，判断为正确翻译
            if LanguageFilter.filter(src, source_language) == True:
                check_result.append(ResponseChecker.Error.NONE)
                continue

            # 当原文中不包含重复文本但是译文中包含重复文本时，判断为 退化
            if ResponseChecker.RE_DEGRADATION.search(src) == None and ResponseChecker.RE_DEGRADATION.search(dst) != None:
                check_result.append(ResponseChecker.Error.LINE_ERROR_DEGRADATION)
                continue

            # 当原文语言为日语，且译文中包含平假名或片假名字符时，判断为 假名残留
            if source_language == BaseLanguage.Enum.JA and (TextHelper.JA.any_hiragana(dst) or TextHelper.JA.any_katakana(dst)):
                check_result.append(ResponseChecker.Error.LINE_ERROR_KANA)
                continue

            # 当原文语言为韩语，且译文中包含谚文字符时，判断为 谚文残留
            if source_language == BaseLanguage.Enum.KO and TextHelper.KO.any_hangeul(dst):
                check_result.append(ResponseChecker.Error.LINE_ERROR_HANGEUL)
                continue

            # 判断是否包含或相似
            if src in dst or dst in src or TextHelper.check_similarity_by_jaccard(src, dst) > 0.80 == True:
                # 日翻中时，只有译文至少包含一个平假名或片假名字符时，才判断为 相似
                if self.source_language == BaseLanguage.Enum.JA and self.target_language == BaseLanguage.Enum.ZH:
                    if TextHelper.JA.any_hiragana(dst) or TextHelper.JA.any_katakana(dst):
                        check_result.append(ResponseChecker.Error.LINE_ERROR_SIMILARITY)
                        continue
                # 韩翻中时，只有译文至少包含一个谚文字符时，才判断为 相似
                elif self.source_language == BaseLanguage.Enum.KO and self.target_language == BaseLanguage.Enum.ZH:
                    if TextHelper.KO.any_hangeul(dst):
                        check_result.append(ResponseChecker.Error.LINE_ERROR_SIMILARITY)
                        continue
                # 其他情况，只要原文译文相同或相似就可以判断为 相似
                else:
                    check_result.append(ResponseChecker.Error.LINE_ERROR_SIMILARITY)
                    continue

            # 默认为无错误
            check_result.append(ResponseChecker.Error.NONE)

        # 返回结果
        return check_result