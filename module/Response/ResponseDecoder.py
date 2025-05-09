import json_repair as repair

from base.Base import Base

class ResponseDecoder(Base):

    def __init__(self) -> None:
        super().__init__()

    # 解析文本
    def decode(self, response: str) -> tuple[dict[str, str], list[dict[str, str]]]:
        dst_dict: dict[str, str] = {}
        glossary_list: list[dict[str, str]] = []

        # 按行解析失败时，尝试按照普通 JSON 字典进行解析
        for line in response.splitlines():
            json_data = repair.loads(line)
            if isinstance(json_data, dict):
                # 翻译结果
                if len(json_data) == 1:
                    _, v = list(json_data.items())[0]
                    if isinstance(v, str):
                        dst_dict[str(len(dst_dict))] = v if isinstance(v, str) else ""

                # 术语表条目
                if len(json_data) == 3:
                    if any(v in json_data for v in ("src", "dst", "gender")):
                        src: str = json_data.get("src")
                        dst: str = json_data.get("dst")
                        gender: str = json_data.get("gender")
                        glossary_list.append(
                            {
                                "src": src if isinstance(src, str) else "",
                                "dst": dst if isinstance(dst, str) else "",
                                "info": gender if isinstance(gender, str) else "",
                            }
                        )

        # 按行解析失败时，尝试按照普通 JSON 字典进行解析
        if len(dst_dict) == 0:
            json_data = repair.loads(response)
            if isinstance(json_data, dict):
                for k, v in json_data.items():
                    if isinstance(v, str):
                        dst_dict[str(len(dst_dict))] = v if isinstance(v, str) else ""

        # 返回默认值
        return dst_dict, glossary_list