import os
import re
import time
import shutil
import threading
import concurrent.futures
from itertools import zip_longest

import httpx
from tqdm import tqdm

from base.Base import Base
from module.Config import Config
from module.File.FileManager import FileManager
from module.Cache.CacheItem import CacheItem
from module.Cache.CacheManager import CacheManager
from module.Filter.RuleFilter import RuleFilter
from module.Filter.LanguageFilter import LanguageFilter
from module.Localizer.Localizer import Localizer
from module.TextPreserver import TextPreserver
from module.Translator.TaskLimiter import TaskLimiter
from module.Translator.TranslatorTask import TranslatorTask
from module.Translator.TranslatorRequester import TranslatorRequester
from module.PromptBuilder import PromptBuilder
from module.ResultChecker import ResultChecker

# 翻译器
class Translator(Base):

    def __init__(self) -> None:
        super().__init__()

        # 初始化
        self.cache_manager = CacheManager(tick = True)

        # 线程锁
        self.data_lock = threading.Lock()

        # 注册事件
        self.subscribe(Base.Event.TRANSLATION_STOP, self.translation_stop)
        self.subscribe(Base.Event.TRANSLATION_START, self.translation_start)
        self.subscribe(Base.Event.TRANSLATION_MANUAL_EXPORT, self.translation_manual_export)
        self.subscribe(Base.Event.PROJECT_STATUS, self.translation_project_status_check)

    # 翻译停止事件
    def translation_stop(self, event: str, data: dict) -> None:
        # 设置运行状态为停止中
        Base.WORK_STATUS = Base.TaskStatus.STOPPING

        def target() -> None:
            while True:
                time.sleep(0.5)
                if self.translating == False:
                    self.print("")
                    self.info(Localizer.get().translator_stop)
                    self.print("")

                    # 设置运行状态
                    Base.WORK_STATUS = Base.TaskStatus.IDLE
                    self.emit(Base.Event.TRANSLATION_STOP_DONE, {})
                    break

        threading.Thread(target = target).start()

    # 翻译开始事件
    def translation_start(self, event: str, data: dict) -> None:
        if Base.WORK_STATUS != Base.TaskStatus.IDLE:
            self.emit(Base.Event.APP_TOAST_SHOW, {
                "type": Base.ToastType.WARNING,
                "message": Localizer.get().translator_running,
            })
        else:
            threading.Thread(
                target = self.translation_start_target,
                args = (data.get("status"), ),
            ).start()

    # 翻译结果手动导出事件
    def translation_manual_export(self, event: str, data: dict) -> None:
        if Base.WORK_STATUS == Base.TaskStatus.TRANSLATING:
            threading.Thread(
                target = self.translation_manual_export_target,
                args = (event, data),
            ).start()

    # 翻译结果手动导出事件
    def translation_manual_export_target(self, event: str, data: dict) -> None:
        # 复制一份以避免影响原始数据
        items = self.cache_manager.copy_items()

        # MTool 优化器后处理
        self.mtool_optimizer_postprocess(items)

        # 检查结果并写入文件
        self.check_and_wirte_result(items)

    # 翻译状态检查事件
    def translation_project_status_check(self, event: str, data: dict) -> None:
        threading.Thread(
            target = self.translation_project_status_check_target
        ).start()

    # 翻译状态检查
    def translation_project_status_check_target(self) -> None:
        # 检查结果的默认值
        status = Base.TranslationStatus.UNTRANSLATED

        # 只有翻译状态为 无任务 时才执行检查逻辑，其他情况直接返回默认值
        if Base.WORK_STATUS == Base.TaskStatus.IDLE:
            cache_manager = CacheManager(tick = False)
            cache_manager.load_project_from_file(Config().load().output_folder)
            status = cache_manager.get_project().get_status()

        self.emit(Base.Event.PROJECT_STATUS_CHECK_DONE, {
            "status" : status,
        })

    # 实际的翻译流程
    def translation_start_target(self, status: int) -> None:
        # 设置内部状态（用于判断翻译任务是否实际在执行）
        self.translating = True

        # 设置翻译状态为正在翻译状态
        Base.WORK_STATUS = Base.TaskStatus.TRANSLATING

        # 初始化
        self.config = Config().load()
        self.platform = self.config.get_platform(self.config.activate_platform)
        local_flag = self.initialize_local_flag()
        max_workers, rpm_threshold = self.initialize_max_workers()

        # 重置文本保护器
        TextPreserver.reset()

        # 重置请求器
        TranslatorRequester.reset()

        # 生成缓存列表
        try:
            # 根据 status 判断是否为继续翻译
            if status == Base.TranslationStatus.TRANSLATING:
                self.cache_manager.load_from_file(self.config.output_folder)
            else:
                shutil.rmtree(f"{self.config.output_folder}/cache", ignore_errors = True)
                project, items = FileManager(self.config).read_from_path()
                self.cache_manager.set_items(items)
                self.cache_manager.set_project(project)
        except Exception as e:
            self.error(f"{Localizer.get().log_read_file_fail}", e)
            return None

        # 检查数据是否为空
        if self.cache_manager.get_item_count() == 0:
            self.emit(Base.Event.APP_TOAST_SHOW, {
                "type": Base.ToastType.WARNING,
                "message": Localizer.get().translator_no_items,
            })

        # 从头翻译时加载默认数据
        if status == Base.TranslationStatus.TRANSLATING:
            self.extras = self.cache_manager.get_project().get_extras()
            self.extras["start_time"] = time.time() - self.extras.get("time", 0)
        else:
            self.extras = {
                "start_time": time.time(),
                "total_line": 0,
                "line": 0,
                "total_tokens": 0,
                "total_output_tokens": 0,
                "time": 0,
            }

        # 更新翻译进度
        self.emit(Base.Event.TRANSLATION_UPDATE, self.extras)

        # 规则过滤
        self.rule_filter(self.cache_manager.get_items())

        # 语言过滤
        self.language_filter(self.cache_manager.get_items())

        # MTool 优化器预处理
        self.mtool_optimizer_preprocess(self.cache_manager.get_items())

        # 开始循环
        for current_round in range(self.config.max_round + 1):
            # 检测是否需要停止任务
            if Base.WORK_STATUS == Base.TaskStatus.STOPPING:
                # 循环次数比实际最大轮次要多一轮，当触发停止翻译的事件时，最后都会从这里退出任务
                # 执行到这里说明停止翻译的任务已经执行完毕，可以重置内部状态了
                self.translating = False
                Base.WORK_STATUS = Base.TaskStatus.IDLE
                return None

            # 获取 待翻译 状态的条目数量
            item_count_status_untranslated = self.cache_manager.get_item_count_by_status(Base.TranslationStatus.UNTRANSLATED)

            # 判断是否需要继续翻译
            if item_count_status_untranslated == 0:
                self.print("")
                self.info(Localizer.get().translator_done)
                self.info(Localizer.get().translator_writing)
                self.print("")
                break

            # 达到最大翻译轮次时
            if item_count_status_untranslated > 0 and current_round == self.config.max_round:
                self.print("")
                self.warning(Localizer.get().translator_fail)
                self.warning(Localizer.get().translator_writing)
                self.print("")
                break

            # 第一轮时且不是继续翻译时，记录总行数
            if current_round == 0 and status == Base.TranslationStatus.UNTRANSLATED:
                self.extras["total_line"] = item_count_status_untranslated

            # 第二轮开始对半切分
            if current_round > 0:
                self.config.token_threshold = max(1, int(self.config.token_threshold / 3))

            # 生成缓存数据条目片段
            chunks, preceding_chunks = self.cache_manager.generate_item_chunks(
                self.config.token_threshold,
                self.config.preceding_lines_threshold,
            )

            # 仅在第一轮启用参考上文功能
            if current_round > 0:
                preceding_chunks = [[] for _ in range(len(preceding_chunks))]

            # 生成翻译任务
            tasks: list[TranslatorTask] = []
            self.print("")
            for items, preceding_items in tqdm(zip(chunks, preceding_chunks), desc = Localizer.get().translator_generate_task, total = len(chunks)):
                tasks.append(
                    TranslatorTask(
                        self.config,
                        self.platform,
                        local_flag,
                        items,
                        preceding_items,
                    )
                )
            self.print("")

            # 输出开始翻译的日志
            self.print("")
            self.info(f"{Localizer.get().translator_current_round} - {current_round + 1}")
            self.info(f"{Localizer.get().translator_max_round} - {self.config.max_round}")
            self.print("")
            self.info(f"{Localizer.get().translator_name} - {self.platform.get("name")}")
            self.info(f"{Localizer.get().translator_api_url} - {self.platform.get("api_url")}")
            self.info(f"{Localizer.get().translator_model} - {self.platform.get("model")}")
            self.print("")
            if self.platform.get("api_format") != Base.APIFormat.SAKURALLM:
                self.info(PromptBuilder(self.config).build_main())
                self.print("")
            self.info(Localizer.get().translator_begin.replace("{TASKS}", str(len(tasks))))
            self.print("")

            # 开始执行翻译任务
            task_limiter = TaskLimiter(rps = max_workers, rpm = rpm_threshold)
            with concurrent.futures.ThreadPoolExecutor(max_workers = max_workers,thread_name_prefix = "translator") as executor:
                for task in tasks:
                    task_limiter.wait()
                    future = executor.submit(task.start, current_round)
                    future.add_done_callback(self.task_done_callback)

        # MTool 优化器后处理
        self.mtool_optimizer_postprocess(self.cache_manager.get_items())

        # 如已完成全部条目的翻译，则设置项目状态为已翻译
        if self.cache_manager.get_item_count_by_status(Base.TranslationStatus.UNTRANSLATED) == 0:
            self.cache_manager.get_project().set_status(Base.TranslationStatus.TRANSLATED)

        # 等待可能存在的缓存文件写入请求处理完毕
        time.sleep(CacheManager.SAVE_INTERVAL)

        # 检查结果并写入文件
        self.check_and_wirte_result(self.cache_manager.get_items())

        # 重置内部状态（正常完成翻译）
        self.translating = False
        Base.WORK_STATUS = Base.TaskStatus.IDLE

        # 触发翻译停止完成的事件
        self.emit(Base.Event.TRANSLATION_STOP_DONE, {})

    # 初始化本地接口标识
    def initialize_local_flag(self) -> bool:
        return re.search(
            r"^http[s]*://localhost|^http[s]*://\d+\.\d+\.\d+\.\d+",
            self.platform.get("api_url"),
            flags = re.IGNORECASE,
        ) is not None

    # 初始化 速度控制
    def initialize_max_workers(self) -> tuple[int, int]:
        max_workers: int = self.config.max_workers
        rpm_threshold: int = self.config.rpm_threshold

        # 当 max_workers = 0 时，尝试获取 llama.cpp 槽数
        if max_workers == 0:
            try:
                response_json = None
                response = httpx.get(re.sub(r"/v1$", "", self.platform.get("api_url")) + "/slots")
                response.raise_for_status()
                response_json = response.json()
            except Exception as e:
                self.print("")
                self.debug(Localizer.get().log_load_llama_cpp_slots_num_fail, e)
            if isinstance(response_json, list) and len(response_json) > 0:
                max_workers = len(response_json)

        if max_workers == 0 and rpm_threshold == 0:
            max_workers = 8
            rpm_threshold = 0
        elif max_workers > 0 and rpm_threshold == 0:
            pass
        elif max_workers == 0 and rpm_threshold > 0:
            max_workers = 8192
            rpm_threshold = rpm_threshold

        return max_workers, rpm_threshold

    # 规则过滤
    def rule_filter(self, items: list[CacheItem]) -> None:
        if len(items) == 0:
            return None

        # 统计排除数量
        self.print("")
        count_excluded = len([v for v in tqdm(items) if v.get_status() == Base.TranslationStatus.EXCLUDED])

        # 筛选出无效条目并标记为已排除
        target = [
            v for v in items
            if RuleFilter.filter(v.get_src(), v.get_skip_internal_filter()) == True
        ]
        for item in target:
            item.set_status(Base.TranslationStatus.EXCLUDED)

        # 输出结果
        count = len([v for v in items if v.get_status() == Base.TranslationStatus.EXCLUDED]) - count_excluded
        self.print("")
        self.info(Localizer.get().translator_rule_filter.replace("{COUNT}", str(count)))

    # 语言过滤
    def language_filter(self, items: list[CacheItem]) -> None:
        if len(items) == 0:
            return None

        # 统计排除数量
        self.print("")
        count_excluded = len([v for v in tqdm(items) if v.get_status() == Base.TranslationStatus.EXCLUDED])

        # 筛选出无效条目并标记为已排除
        source_language = self.config.source_language
        target = [
            v for v in items
            if LanguageFilter.filter(v.get_src(), source_language) == True
        ]
        for item in target:
            item.set_status(Base.TranslationStatus.EXCLUDED)

        # 输出结果
        count = len([v for v in items if v.get_status() == Base.TranslationStatus.EXCLUDED]) - count_excluded
        self.print("")
        self.info(Localizer.get().translator_language_filter.replace("{COUNT}", str(count)))

    # MTool 优化器预处理
    def mtool_optimizer_preprocess(self, items: list[CacheItem]) -> None:
        if len(items) == 0 or self.config.mtool_optimizer_enable == False:
            return None

        # 统计排除数量
        self.print("")
        count_excluded = len([v for v in tqdm(items) if v.get_status() == Base.TranslationStatus.EXCLUDED])

        # 筛选
        items_kvjson = [item for item in items if item.get_file_type() == CacheItem.FileType.KVJSON]

        # 按文件路径分组
        group_by_file_path: dict[str, list[CacheItem]] = {}
        for item in items_kvjson:
            group_by_file_path.setdefault(item.get_file_path(), []).append(item)

        # 分别处理每个文件的数据
        for items_by_file_path in group_by_file_path.values():
            # 找出子句
            target = set()
            for item in items_by_file_path:
                src = item.get_src()
                if src.count("\n") > 0:
                    target.update([line.strip() for line in src.splitlines() if line.strip() != ""])

            # 移除子句
            for item in items_by_file_path:
                if item.get_src() in target:
                    item.set_status(Base.TranslationStatus.EXCLUDED)

        count = len([v for v in items if v.get_status() == Base.TranslationStatus.EXCLUDED]) - count_excluded
        self.print("")
        self.info(Localizer.get().translator_mtool_filter.replace("{COUNT}", str(count)))

    # MTool 优化器后处理
    def mtool_optimizer_postprocess(self, items: list[CacheItem]) -> None:
        if len(items) == 0 or self.config.mtool_optimizer_enable == False:
            return None

        # 筛选
        items_kvjson = [item for item in items if item.get_file_type() == CacheItem.FileType.KVJSON]

        # 按文件路径分组
        group_by_file_path: dict[str, list[CacheItem]] = {}
        for item in items_kvjson:
            group_by_file_path.setdefault(item.get_file_path(), []).append(item)

        # 分别处理每个文件的数据
        for items_by_file_path in group_by_file_path.values():
            for item in items_by_file_path:
                src = item.get_src()
                dst = item.get_dst()
                if src.count("\n") > 0:
                    for src_line, dst_line in zip_longest(src.splitlines(), dst.splitlines(), fillvalue = ""):
                        item_ex = CacheItem(item.get_vars())
                        item_ex.set_src(src_line.strip())
                        item_ex.set_dst(dst_line.strip())
                        item_ex.set_row(len(items_by_file_path))
                        items.append(item_ex)

    # 检查结果并写入文件
    def check_and_wirte_result(self, items: list[CacheItem]) -> None:
        # 启用自动术语表的时，更新配置文件
        if self.config.glossary_enable == True and self.config.auto_glossary_enable == True:
            # 更新配置文件
            config = Config().load()
            config.glossary_data = self.config.glossary_data
            config.save()

            # 术语表刷新事件
            self.emit(Base.Event.GLOSSARY_REFRESH, {})

        # 检查结果
        ResultChecker(self.config, items).check()

        # 写入文件
        FileManager(self.config).write_to_path(items)
        self.print("")
        self.info(Localizer.get().translator_write.replace("{PATH}", self.config.output_folder))
        self.print("")

    # 翻译任务完成时
    def task_done_callback(self, future: concurrent.futures.Future) -> None:
        try:
            # 获取结果
            result = future.result()

            # 结果为空则跳过后续的更新步骤
            if not isinstance(result, dict) or len(result) == 0:
                return

            # 记录数据
            with self.data_lock:
                new = {}
                new["start_time"] = self.extras.get("start_time", 0)
                new["total_line"] = self.extras.get("total_line", 0)
                new["line"] = self.extras.get("line", 0) + result.get("row_count", 0)
                new["total_tokens"] = self.extras.get("total_tokens", 0) + result.get("input_tokens", 0) + result.get("output_tokens", 0)
                new["total_output_tokens"] = self.extras.get("total_output_tokens", 0) + result.get("output_tokens", 0)
                new["time"] = time.time() - self.extras.get("start_time", 0)
                self.extras = new

            # 更新翻译进度
            self.cache_manager.get_project().set_extras(self.extras)

            # 更新翻译状态
            self.cache_manager.get_project().set_status(Base.TranslationStatus.TRANSLATING)

            # 请求保存缓存文件
            self.cache_manager.require_save_to_file(self.config.output_folder)

            # 触发翻译进度更新事件
            self.emit(Base.Event.TRANSLATION_UPDATE, self.extras)
        except Exception as e:
            self.error(f"{Localizer.get().log_task_fail}", e)