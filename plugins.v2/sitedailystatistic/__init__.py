import warnings
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from app.helper.sites import SitesHelper
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.models.siteuserdata import SiteUserData
from app.schemas import SiteUserData as SSiteUserData
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.string import StringUtils

warnings.filterwarnings("ignore", category=FutureWarning)

lock = Lock()


class SiteDailyStatistic(_PluginBase):
    # 插件名称
    plugin_name = "站点每日数据统计"
    # 插件描述
    plugin_desc = "站点统计数据图表及当天累计站点数据通知"
    # 插件图标
    plugin_icon = "Collabora_A.png"
    # 插件版本
    plugin_version = "3.8"
    # 插件作者
    plugin_author = "Xiang"
    # 作者主页
    author_url = "https://github.com/xiangt920"
    # 插件配置项ID前缀
    plugin_config_prefix = "sitedailystatistic_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 配置属性
    siteoper = None
    siteshelper = None
    sitechain = None
    _enabled: bool = False
    _onlyonce: bool = False
    _dashboard_type: str = "today"
    _notify_type = ""
    _scheduler = None

    def init_plugin(self, config: dict = None):
        self.siteoper = SiteOper()
        self.siteshelper = SitesHelper()
        self.sitechain = SiteChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._dashboard_type = config.get("dashboard_type") or "today"
            self._notify_type = config.get("notify_type") or ""

        if self._onlyonce:
            config["onlyonce"] = False
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(self.sitechain.refresh_userdatas, "date",
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="站点数据统计服务")
            self._scheduler.print_jobs()
            self._scheduler.start()
            self.update_config(config=config)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/refresh_daily_by_domain",
            "endpoint": self.refresh_by_domain,
            "methods": ["GET"],
            "summary": "刷新站点今日数据",
            "description": "刷新对应域名的站点今日数据",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        ret_jobs = []
        # 添加一个每日0点的统计任务，仅在该任务中保存数据
        ret_jobs.append({
            "id": "SiteDailyStatistic00",
                "name": "站点每日数据统计服务",
                "trigger": CronTrigger.from_crontab('59 23 * * *'),
                "func": self.refresh_all_sites,
                "kwargs": {}
        })
        return ret_jobs

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'dashboard_type',
                                            'label': '仪表板组件',
                                            'items': [
                                                {'title': '今日数据', 'value': 'today'},
                                                {'title': '汇总数据', 'value': 'total'},
                                                {'title': '所有数据', 'value': 'all'}
                                            ]
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
                                            'model': 'notify_type',
                                            'label': '数据刷新时发送通知',
                                            'items': [
                                                {'title': '不发送', 'value': ''},
                                                {'title': '今日增量数据', 'value': 'inc'},
                                                {'title': '累计全量数据', 'value': 'all'}
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "dashboard_type": 'today'
        }

    @eventmanager.register(EventType.SiteRefreshed)
    def send_msg(self, event: Event):
        """
        站点数据刷新事件时发送消息
        """
        if not self._notify_type:
            return
        if event.event_data.get('site_id') != "*":
            return
        # 获取站点数据
        today, today_data, yesterday_data = self.__get_data()
        # 转换为字典
        today_data_dict = {data.name: data for data in today_data}
        yesterday_data_dict = {data.name: data for data in yesterday_data}
        # 消息内容
        messages = {}
        # 总上传
        incUploads = 0
        # 总下载
        incDownloads = 0
        # 今天的日期
        today_date = datetime.now().strftime("%Y-%m-%d")

        for rand, site in enumerate(today_data_dict.keys()):
            upload = int(today_data_dict[site].upload or 0)
            download = int(today_data_dict[site].download or 0)
            updated_date = today_data_dict[site].updated_day

            if self._notify_type == "inc" and yesterday_data_dict.get(site):
                upload -= int(yesterday_data_dict[site].upload or 0)
                download -= int(yesterday_data_dict[site].download or 0)

            if updated_date and updated_date != today_date:
                updated_date = f"（{updated_date}）"
            else:
                updated_date = ""

            if upload > 0 or download > 0:
                incUploads += upload
                incDownloads += download
                messages[upload + (rand / 1000)] = (
                        f"【{site}】{updated_date}\n"
                        + f"上传量：{StringUtils.str_filesize(upload)}\n"
                        + f"下载量：{StringUtils.str_filesize(download)}\n"
                        + "————————————"
                )

        if incDownloads or incUploads:
            sorted_messages = [messages[key] for key in sorted(messages.keys(), reverse=True)]
            sorted_messages.insert(0, f"【汇总】\n"
                                      f"总上传：{StringUtils.str_filesize(incUploads)}\n"
                                      f"总下载：{StringUtils.str_filesize(incDownloads)}\n"
                                      f"————————————")
            self.post_message(mtype=NotificationType.SiteMessage,
                              title="站点数据统计", text="\n".join(sorted_messages))

    def __get_data(self) -> Tuple[str, List[SiteUserData], List[SiteUserData]]:
        """
        获取今天的日期、今天的站点数据、昨天的站点数据
        """
        # 获取最近所有数据
        data_list: List[SiteUserData] = self.siteoper.get_userdata()
        if not data_list:
            return "", [], []
        # 每个日期、每个站点只保留最后一条数据
        data_list = list({f"{data.updated_day}_{data.name}": data for data in data_list}.values())
        # 按日期倒序排序
        data_list.sort(key=lambda x: x.updated_day, reverse=True)
        # 获取今天的日期
        today = data_list[0].updated_day
        # 获取昨天的日期
        yestoday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        # 今天的数据
        stattistic_data = [data for data in data_list if data.updated_day == today]
        # 今日数据按数据量降序排序
        stattistic_data.sort(key=lambda x: x.upload, reverse=True)
        # 昨天的数据
        yesterday_sites_data = [data for data in data_list if data.updated_day == yestoday]

        return today, stattistic_data, yesterday_sites_data

    @staticmethod
    def __get_total_elements(today: str, stattistic_data: List[SiteUserData], yesterday_sites_data: List[SiteUserData],
                             dashboard: str = "today") -> List[dict]:
        """
        获取统计元素
        """

        def __gb(value: int) -> float:
            """
            转换为GB，保留1位小数
            """
            if not value:
                return 0
            return round(float(value) / 1024 / 1024 / 1024, 1)

        def __is_digit(value: any) -> bool:
            """
            判断是否为数字
            """
            if value is None:
                return False
            if isinstance(value, float) or isinstance(value, int):
                return True
            if isinstance(value, str):
                return value.isdigit()
            return False

        def __to_numeric(value: any) -> int:
            """
            将值转换为整数
            """
            if isinstance(value, str):
                return int(float(value))
            elif isinstance(value, float) or isinstance(value, int):
                return int(value)
            else:
                logger.error(f'数据类型转换错误 ({value})')
                return 0

        def __sub_data(d1: dict, d2: dict) -> dict:
            """
            计算两个字典相同Key值的差值（如果值为数字），返回新字典
            """
            if not d1:
                return {}
            if not d2:
                return d1
            d = {k: __to_numeric(d1.get(k)) - __to_numeric(d2.get(k)) for k in d1
                 if k in d2 and __is_digit(d1.get(k)) and __is_digit(d2.get(k))}
            # 把小于0的数据变成0
            for k, v in d.items():
                if str(v).isdigit() and int(v) < 0:
                    d[k] = 0
            return d

        if dashboard in ['total', 'all']:
            # 总上传量
            total_upload = sum([data.upload for data in stattistic_data if data.upload])
            # 总下载量
            total_download = sum([data.download for data in stattistic_data if data.download])
            # 总做种数
            total_seed = sum([data.seeding for data in stattistic_data if data.seeding])
            # 总做种体积
            total_seed_size = sum([data.seeding_size for data in stattistic_data if data.seeding_size])

            total_elements = [
                # 总上传量
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 6,
                        'md': 3
                    },
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {
                                'variant': 'tonal',
                            },
                            'content': [
                                {
                                    'component': 'VCardText',
                                    'props': {
                                        'class': 'd-flex align-center',
                                    },
                                    'content': [
                                        {
                                            'component': 'VAvatar',
                                            'props': {
                                                'rounded': True,
                                                'variant': 'text',
                                                'class': 'me-3'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VImg',
                                                    'props': {
                                                        'src': './plugin_icon/upload.png'
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'div',
                                            'content': [
                                                {
                                                    'component': 'span',
                                                    'props': {
                                                        'class': 'text-caption'
                                                    },
                                                    'text': '总上传量'
                                                },
                                                {
                                                    'component': 'div',
                                                    'props': {
                                                        'class': 'd-flex align-center flex-wrap'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-h6'
                                                            },
                                                            'text': StringUtils.str_filesize(total_upload)
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                    ]
                },
                # 总下载量
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 6,
                        'md': 3,
                    },
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {
                                'variant': 'tonal',
                            },
                            'content': [
                                {
                                    'component': 'VCardText',
                                    'props': {
                                        'class': 'd-flex align-center',
                                    },
                                    'content': [
                                        {
                                            'component': 'VAvatar',
                                            'props': {
                                                'rounded': True,
                                                'variant': 'text',
                                                'class': 'me-3'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VImg',
                                                    'props': {
                                                        'src': './plugin_icon/download.png'
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'div',
                                            'content': [
                                                {
                                                    'component': 'span',
                                                    'props': {
                                                        'class': 'text-caption'
                                                    },
                                                    'text': '总下载量'
                                                },
                                                {
                                                    'component': 'div',
                                                    'props': {
                                                        'class': 'd-flex align-center flex-wrap'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-h6'
                                                            },
                                                            'text': StringUtils.str_filesize(total_download)
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                    ]
                },
                # 总做种数
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 6,
                        'md': 3
                    },
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {
                                'variant': 'tonal',
                            },
                            'content': [
                                {
                                    'component': 'VCardText',
                                    'props': {
                                        'class': 'd-flex align-center',
                                    },
                                    'content': [
                                        {
                                            'component': 'VAvatar',
                                            'props': {
                                                'rounded': True,
                                                'variant': 'text',
                                                'class': 'me-3'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VImg',
                                                    'props': {
                                                        'src': './plugin_icon/seed.png'
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'div',
                                            'content': [
                                                {
                                                    'component': 'span',
                                                    'props': {
                                                        'class': 'text-caption'
                                                    },
                                                    'text': '总做种数'
                                                },
                                                {
                                                    'component': 'div',
                                                    'props': {
                                                        'class': 'd-flex align-center flex-wrap'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-h6'
                                                            },
                                                            'text': f'{"{:,}".format(total_seed)}'
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                    ]
                },
                # 总做种体积
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 6,
                        'md': 3
                    },
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {
                                'variant': 'tonal',
                            },
                            'content': [
                                {
                                    'component': 'VCardText',
                                    'props': {
                                        'class': 'd-flex align-center',
                                    },
                                    'content': [
                                        {
                                            'component': 'VAvatar',
                                            'props': {
                                                'rounded': True,
                                                'variant': 'text',
                                                'class': 'me-3'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VImg',
                                                    'props': {
                                                        'src': './plugin_icon/database.png'
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'div',
                                            'content': [
                                                {
                                                    'component': 'span',
                                                    'props': {
                                                        'class': 'text-caption'
                                                    },
                                                    'text': '总做种体积'
                                                },
                                                {
                                                    'component': 'div',
                                                    'props': {
                                                        'class': 'd-flex align-center flex-wrap'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-h6'
                                                            },
                                                            'text': StringUtils.str_filesize(total_seed_size)
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        else:
            total_elements = []

        if dashboard in ["today", "all"]:
            # 计算增量数据集
            inc_data = {}
            for data in stattistic_data:
                yesterday_datas = [yd for yd in yesterday_sites_data if yd.domain == data.domain]
                if yesterday_datas:
                    yesterday_data = yesterday_datas[0]
                else:
                    yesterday_data = None
                inc = __sub_data(data.to_dict(), yesterday_data.to_dict() if yesterday_data else None)
                if inc:
                    inc_data[data.name] = inc
            # 今日上传
            uploads = {k: v for k, v in inc_data.items() if v.get("upload") if v.get("upload") > 0}
            # 今日上传站点
            upload_sites = [site for site in uploads.keys()]
            # 今日上传数据
            upload_datas = [__gb(data.get("upload")) for data in uploads.values()]
            # 今日上传总量
            today_upload = round(sum(upload_datas), 2)
            # 今日下载
            downloads = {k: v for k, v in inc_data.items() if v.get("download") if v.get("download") > 0}
            # 今日下载站点
            download_sites = [site for site in downloads.keys()]
            # 今日下载数据
            download_datas = [__gb(data.get("download")) for data in downloads.values()]
            # 今日下载总量
            today_download = round(sum(download_datas), 2)
            # 今日上传下载元素
            today_elements = [
                # 上传量图表
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 12,
                        'md': 6
                    },
                    'content': [
                        {
                            'component': 'VApexChart',
                            'props': {
                                'height': 300,
                                'options': {
                                    'chart': {
                                        'type': 'pie',
                                    },
                                    'labels': upload_sites,
                                    'title': {
                                        'text': f'今日上传（{today}）共 {today_upload} GB'
                                    },
                                    'legend': {
                                        'show': True
                                    },
                                    'plotOptions': {
                                        'pie': {
                                            'expandOnClick': False
                                        }
                                    },
                                    'noData': {
                                        'text': '暂无数据'
                                    }
                                },
                                'series': upload_datas
                            }
                        }
                    ]
                },
                # 下载量图表
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 12,
                        'md': 6
                    },
                    'content': [
                        {
                            'component': 'VApexChart',
                            'props': {
                                'height': 300,
                                'options': {
                                    'chart': {
                                        'type': 'pie',
                                    },
                                    'labels': download_sites,
                                    'title': {
                                        'text': f'今日下载（{today}）共 {today_download} GB'
                                    },
                                    'legend': {
                                        'show': True
                                    },
                                    'plotOptions': {
                                        'pie': {
                                            'expandOnClick': False
                                        }
                                    },
                                    'noData': {
                                        'text': '暂无数据'
                                    }
                                },
                                'series': download_datas
                            }
                        }
                    ]
                }
            ]
        else:
            today_elements = []
        # 合并返回
        return total_elements + today_elements

    def get_dashboard(self, key: str, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        """
        获取插件仪表盘页面，需要返回：1、仪表板col配置字典；2、仪表板页面元素配置json（含数据）；3、全局配置（自动刷新等）
        1、col配置参考：
        {
            "cols": 12, "md": 6
        }
        2、页面配置使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        3、全局配置参考：
        {
            "refresh": 10 // 自动刷新时间，单位秒
        }
        """
        # 列配置
        cols = {
            "cols": 12
        }
        # 全局配置
        attrs = {}
        # 获取数据
        today, stattistic_data, yesterday_sites_data = self.__get_data()
        # 汇总
        # 站点统计
        elements = [
            {
                'component': 'VRow',
                'content': self.__get_total_elements(
                    today=today,
                    stattistic_data=stattistic_data,
                    yesterday_sites_data=yesterday_sites_data,
                    dashboard=self._dashboard_type
                )
            }
        ]
        return cols, attrs, elements

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """

        def format_bonus(bonus):
            try:
                return f'{float(bonus):,.1f}'
            except ValueError:
                return '0.0'

        # 获取数据
        today, stattistic_data, yesterday_sites_data = self.__get_data()
        if not stattistic_data:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        # 站点统计
        site_totals = self.__get_total_elements(
            today=today,
            stattistic_data=stattistic_data,
            yesterday_sites_data=yesterday_sites_data,
            dashboard='all'
        )

        # 站点数据明细
        site_trs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': data.name
                    },
                    {
                        'component': 'td',
                        'text': data.username
                    },
                    {
                        'component': 'td',
                        'text': data.user_level
                    },
                    {
                        'component': 'td',
                        'props': {
                            'class': 'text-success'
                        },
                        'text': StringUtils.str_filesize(data.upload)
                    },
                    {
                        'component': 'td',
                        'props': {
                            'class': 'text-error'
                        },
                        'text': StringUtils.str_filesize(data.download)
                    },
                    {
                        'component': 'td',
                        'text': data.ratio
                    },
                    {
                        'component': 'td',
                        'text': format_bonus(data.bonus or 0)
                    },
                    {
                        'component': 'td',
                        'text': data.seeding
                    },
                    {
                        'component': 'td',
                        'text': StringUtils.str_filesize(data.seeding_size)
                    }
                ]
            } for data in stattistic_data
        ]

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': site_totals + [
                    # 各站点数据明细
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '站点'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '用户名'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '用户等级'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '上传量'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '下载量'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '分享率'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '魔力值'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '做种数'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '做种体积'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': site_trs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def refresh_by_domain(self, domain: str, apikey: str) -> schemas.Response:
        """
        刷新一个站点数据，可由API调用
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        site_info = self.siteshelper.get_indexer(domain)
        if site_info:
            site_data = SiteChain().refresh_userdata(site=site_info)
            if site_data:
                return schemas.Response(
                    success=True,
                    message=f"站点 {domain} 刷新成功",
                    data=site_data.dict()
                )
            return schemas.Response(
                success=False,
                message=f"站点 {domain} 刷新数据失败，未获取到数据"
            )
        return schemas.Response(
            success=False,
            message=f"站点 {domain} 不存在"
        )
    
    def refresh_all_sites(self):
        # 获取最近所有数据
        data_list: List[SiteUserData] = self.siteoper.get_userdata()
        if data_list:
            # 获取所有站点名称
            all_sites = list({f"{data.name}" : data.updated_day for data in data_list}.keys())
            # 每个日期、每个站点只保留最后一条数据
            data_list = list({f"{data.updated_day}_{data.name}": data for data in data_list}.values())
            # 按日期倒序排序
            data_list.sort(key=lambda x: x.updated_day, reverse=True)
            # 获取今天的日期
            today = data_list[0].updated_day
            # 如果站点没有今天的数据，就用昨天的数据填充为今天的数据
            for site in all_sites:
                site_data_list = [data for data in data_list if data.name == site]
                if site_data_list and site_data_list[0].updated_day != today:
                    newest_site_data = site_data_list[0]
                    logger.info(f"站点{newest_site_data.name}没有今日（{today}）数据，开始以 {newest_site_data.updated_day} 的数据填充")
                    payload = SSiteUserData(
                        domain=newest_site_data.domain,
                        userid=newest_site_data.userid,
                        username=newest_site_data.username,
                        user_level=newest_site_data.user_level,
                        join_at=newest_site_data.join_at,
                        upload=newest_site_data.upload,
                        download=newest_site_data.download,
                        ratio=newest_site_data.ratio,
                        bonus=newest_site_data.bonus,
                        seeding=newest_site_data.seeding,
                        seeding_size=newest_site_data.seeding_size,
                        seeding_info=newest_site_data.seeding_info or [],
                        leeching=newest_site_data.leeching,
                        leeching_size=newest_site_data.leeching_size,
                        message_unread=newest_site_data.message_unread,
                        message_unread_contents=newest_site_data.message_unread_contents or [],
                        updated_day=newest_site_data.today,
                        err_msg=newest_site_data.err_msg
                    ).dict()
                    self.siteoper.update_userdata(domain=newest_site_data.domain,
                                          name=newest_site_data.name,
                                          payload=payload)
                    logger.info(f"站点{newest_site_data.name}没有今日（{today}）数据，以 {newest_site_data.updated_day} 的数据填充成功")
        self.sitechain.refresh_userdatas()