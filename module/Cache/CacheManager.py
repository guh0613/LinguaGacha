import os
import time
import json
import threading

from base.Base import Base
from module.Cache.CacheItem import CacheItem
from module.Cache.CacheProject import CacheProject
from module.Localizer.Localizer import Localizer

class CacheManager(Base):

    # 缓存文件保存周期（秒）
    SAVE_INTERVAL = 15

    # 结尾标点符号
    END_LINE_PUNCTUATION = (
        ".",
        "。",
        "?",
        "？",
        "!",
        "！",
        "…",
        "'",
        "\"",
        "’",
        "”",
        "」",
        "』",
    )

    # 类线程锁
    FILE_LOCK = threading.Lock()

    def __init__(self, tick: bool) -> None:
        super().__init__()

        # 默认值
        self.project: CacheProject = CacheProject({})
        self.items: list[CacheItem] = []

        # 启动定时任务
        if tick == True:
            threading.Thread(target = self.save_to_file_tick).start()

    # 保存缓存到文件
    def save_to_file(self, project: CacheProject = None, items: list[CacheItem] = None, output_folder: str = None) -> None:
        # 创建上级文件夹
        os.makedirs(f"{output_folder}/cache", exist_ok = True)

        # 保存缓存到文件
        path = f"{output_folder}/cache/items.json"
        with CacheManager.FILE_LOCK:
            try:
                with open(path, "w", encoding = "utf-8") as writer:
                    writer.write(json.dumps([item.get_vars() for item in items], indent = None, ensure_ascii = False))
            except Exception as e:
                self.debug(Localizer.get().log_write_cache_file_fail, e)

        # 保存项目数据到文件
        path = f"{output_folder}/cache/project.json"
        with CacheManager.FILE_LOCK:
            try:
                with open(path, "w", encoding = "utf-8") as writer:
                    writer.write(json.dumps(project.get_vars(), indent = None, ensure_ascii = False))
            except Exception as e:
                self.debug(Localizer.get().log_write_cache_file_fail, e)

    # 保存缓存到文件的定时任务
    def save_to_file_tick(self) -> None:
        while True:
            time.sleep(__class__.SAVE_INTERVAL)

            # 接收到保存信号则保存
            if getattr(self, "save_to_file_require_flag", False)  == True:
                # 创建上级文件夹
                folder_path = f"{self.save_to_file_require_path}/cache"
                os.makedirs(folder_path, exist_ok = True)

                # 保存缓存到文件
                self.save_to_file(
                    project = self.project,
                    items = self.items,
                    output_folder = self.save_to_file_require_path,
                )

                # 触发事件
                self.emit(Base.Event.CACHE_FILE_AUTO_SAVE, {})

                # 重置标志
                self.save_to_file_require_flag = False

    # 请求保存缓存到文件
    def require_save_to_file(self, output_path: str) -> None:
        self.save_to_file_require_flag = True
        self.save_to_file_require_path = output_path

    # 从文件读取数据
    def load_from_file(self, output_path: str) -> None:
        path = f"{output_path}/cache/items.json"
        with CacheManager.FILE_LOCK:
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding = "utf-8-sig") as reader:
                        self.items = [CacheItem(item) for item in json.load(reader)]
            except Exception as e:
                self.debug(Localizer.get().log_read_cache_file_fail, e)

        path = f"{output_path}/cache/project.json"
        with CacheManager.FILE_LOCK:
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding = "utf-8-sig") as reader:
                        self.project = CacheProject(json.load(reader))
            except Exception as e:
                self.debug(Localizer.get().log_read_cache_file_fail, e)

    # 从文件读取项目数据
    def load_project_from_file(self, output_path: str) -> None:
        path = f"{output_path}/cache/project.json"
        with CacheManager.FILE_LOCK:
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding = "utf-8-sig") as reader:
                        self.project = CacheProject(json.load(reader))
            except Exception as e:
                self.debug(Localizer.get().log_read_cache_file_fail, e)

    # 设置缓存数据
    def set_items(self, items: list[CacheItem]) -> None:
        self.items = items

    # 获取缓存数据
    def get_items(self) -> list[CacheItem]:
        return self.items

    # 设置项目数据
    def set_project(self, project: CacheProject) -> None:
        self.project = project

    # 获取项目数据
    def get_project(self) -> CacheProject:
        return self.project

    # 获取缓存数据数量
    def get_item_count(self) -> int:
        return len(self.items)

    # 复制缓存数据
    def copy_items(self) -> list[CacheItem]:
        return [CacheItem(item.get_vars()) for item in self.items]

    # 获取缓存数据数量（根据翻译状态）
    def get_item_count_by_status(self, status: int) -> int:
        return len([item for item in self.items if item.get_status() == status])

    # 生成缓存数据条目片段
    def generate_item_chunks(self, token_threshold: int, preceding_lines_threshold: int) -> list[list[CacheItem]]:
        # 根据 Token 阈值计算行数阈值，避免大量短句导致行数太多
        line_limit = max(8, int(token_threshold / 16))

        skip: int = 0
        line_length: int = 0
        token_length: int = 0
        chunk: list[CacheItem] = []
        chunks: list[list[CacheItem]] = []
        preceding_chunks: list[list[CacheItem]] = []
        for i, item in enumerate(self.items):
            # 跳过状态不是 未翻译 的数据
            if item.get_status() != Base.TranslationStatus.UNTRANSLATED:
                skip = skip + 1
                continue

            # 每个片段的第一条不判断是否超限，以避免特别长的文本导致死循环
            current_line_length = sum(1 for line in item.get_src().splitlines() if line.strip())
            current_token_length = item.get_token_count()
            if len(chunk) == 0:
                pass
            # 如果 行数超限、Token 超限、数据来源跨文件，则结束此片段
            elif (
                line_length + current_line_length > line_limit
                or token_length + current_token_length > token_threshold
                or item.get_file_path() != chunk[-1].get_file_path()
            ):
                chunks.append(chunk)
                preceding_chunks.append(self.generate_preceding_chunks(chunk, i, skip, preceding_lines_threshold))
                skip = 0

                chunk = []
                line_length = 0
                token_length = 0

            chunk.append(item)
            line_length = line_length + current_line_length
            token_length = token_length + current_token_length

        # 如果还有剩余数据，则添加到列表中
        if len(chunk) > 0:
            chunks.append(chunk)
            preceding_chunks.append(self.generate_preceding_chunks(chunk, i + 1, skip, preceding_lines_threshold))
            skip = 0

        return chunks, preceding_chunks

    # 生成参考上文数据条目片段
    def generate_preceding_chunks(self, chunk: list[CacheItem], start: int, skip: int, preceding_lines_threshold: int) -> list[list[CacheItem]]:
        result: list[CacheItem] = []

        for i in range(start - skip - len(chunk) - 1, -1, -1):
            item = self.items[i]

            # 跳过 已排除 的数据
            if item.get_status() == Base.TranslationStatus.EXCLUDED:
                continue

            # 跳过空数据
            src = item.get_src().strip()
            if src == "":
                continue

            # 候选数据超过阈值时，结束搜索
            if len(result) >= preceding_lines_threshold:
                break

            # 候选数据与当前任务不在同一个文件时，结束搜索
            if item.get_file_path() != chunk[-1].get_file_path():
                break

            # 候选数据以指定标点结尾时，添加到结果中
            if src.endswith(CacheManager.END_LINE_PUNCTUATION):
                result.append(item)
            else:
                break

        # 简单逆序
        return result[::-1]