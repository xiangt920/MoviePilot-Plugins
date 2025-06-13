from typing import Any, List, Dict, Tuple
from urllib.parse import quote_plus

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
import re


class XiangHookMsg(_PluginBase):
    # 插件名称
    plugin_name = "Xiang Webhook通知"
    # 插件描述
    plugin_desc = "支持使用Webhook发送消息通知。"
    # 插件图标
    plugin_icon = "Rocketchat_A.png"
    # 插件版本
    plugin_version = "2.1"
    # 插件作者
    plugin_author = "Xiang"
    # 作者主页
    author_url = "https://github.com/xiangt920"
    # 插件配置项ID前缀
    plugin_config_prefix = "xiang_hook_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1
    # 换行符替换正则
    new_line_after_2space_pattern = re.compile(' *\n')

    # 私有属性
    _enabled = False
    _server = None
    _apikey = None
    _msgtypes = []
    _subpath = '/hooks'
    # convert '\n' to "  \n"(add two spaces)
    _breaks = False 

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._msgtypes = config.get("msgtypes") or []
            self._server = config.get("server")
            self._apikey = config.get("apikey")
            self._breaks = config.get("breaks") or False
            self._subpath = config.get("subPath") or "/hooks"
        if self._subpath.endswith('/') or self._subpath.startswith('/'):
            self._subpath = self._subpath.strip('/')


    def get_state(self) -> bool:
        return self._enabled and (True if self._server and self._apikey else False)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }, 
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'breaks',
                                            'label': '硬换行',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'server',
                                            'label': '服务器URL',
                                            'placeholder': 'https://api.day.app',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'subPath',
                                            'label': '路径',
                                            'placeholder': 'URL子路径，默认为/hooks',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'apikey',
                                            'label': 'token',
                                            'placeholder': '访问token',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'msgtypes',
                                            'label': '消息类型',
                                            'items': MsgTypeOptions
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            'breaks': False,
            'msgtypes': [],
            'server': 'https://api.day.app',
            'apikey': '',
            'subPath': '/hooks'
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        """
        消息发送事件
        """
        if not self.get_state():
            return

        if not event.event_data:
            return

        msg_body = event.event_data
        # 类型
        msg_type: NotificationType = msg_body.get("type")
        # 标题
        title = msg_body.get("title")
        # 文本
        text = msg_body.get("text")

        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return

        if (msg_type and self._msgtypes
                and msg_type.name not in self._msgtypes):
            logger.info(f"消息类型 {msg_type.value} 未开启消息发送")
            return

        try:
            if not self._server or not self._apikey:
                return False, "参数未配置"
            rc_url = "%s/%s/%s" % (self._server, self._subpath, self._apikey)
            rc_text = None
            if title:
                if msg_body.get("link"):
                    rc_text = "[%s](%s) \n" % (title, msg_body.get("link"))
                else:
                    rc_text = "# %s \n" % title
            if text:
                rc_text = "%s%s \n" % (rc_text, text)
            if self._breaks:
                trim_text = self.new_line_after_2space_pattern.sub('\n', rc_text)
                rc_text = str.replace(trim_text, '\n', '  \n')
            image = msg_body.get("image") or ""
            rc_data = {
                "text": rc_text,
                "image": image
            }
            logger.info(f"发送消息至{rc_url}, 图片：{image}, 内容：{rc_text}")
            res = RequestUtils(headers={
                },content_type="application/json").post_res(rc_url, json=rc_data)
            if res and res.status_code == 200:
                logger.info("发送成功")
            elif res is not None:
                logger.warn(f"错误码：{res.status_code}，错误原因：{res.reason}")
            else:
                logger.warn("未获取到返回信息")

        except Exception as msg_e:
            logger.error(f"Xiang.Chat消息发送失败：{str(msg_e)}")

    def stop_service(self):
        """
        退出插件
        """
        pass
