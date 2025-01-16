import subprocess
import json
import re
import traceback
import warnings
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

from fastapi import Depends
import pytz
import requests
from ruamel.yaml import CommentedMap

from app import schemas
from app.core.config import settings
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.event import Event
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.db.models.user import User
from app.db.site_oper import SiteOper
from app.db.user_oper import get_current_active_user
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType
from app.chain.search import SearchChain
from app.utils.string import StringUtils

lock = Lock()

class TorrentSearch(_PluginBase):
    # 插件名称
    plugin_name = "搜索种子"
    # 插件描述
    plugin_desc = "直接搜索指定站点的种子。"
    # 插件图标
    plugin_icon = "Searxng_A.png"
    # 插件版本
    plugin_version = "2.0"
    # 插件作者
    plugin_author = "Xiang"
    # 作者主页
    author_url = "https://github.com/xiangt920"
    # 插件配置项ID前缀
    plugin_config_prefix = "torrentsearch_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    sites = None
    siteoper = None
    _last_update_time: Optional[datetime] = None
    _torrent_data: list = []
    # 正则表达式
    _pattern_progress_start = re.compile('^\{(.*\(\));')
    _pattern_progress_end = re.compile('\)\{}(.{1,20}\(\))}}$')

    # 配置属性
    _enabled: bool = False
    _notify: bool = False
    _search_sites: list = []
    _search_key: str = ""
    _download_path: str = ""

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._search_key = config.get("search_key")
            self._download_path = config.get("download_path")
            self._search_sites = config.get("search_sites")

            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in
                                                                                   self.__custom_sites()]
            self._search_sites = [site_id for site_id in all_sites if site_id in self._search_sites]
            self.__update_config()

        if self._enabled and bool(self._search_key.strip(' \n\r\t')) :
            # 种子数据
            self._torrent_data = []

            logger.info(f"开始从站点搜索种子")
            try:
                self.search_torrent()
            except Exception as e:
                logger.error(f"搜索种子发生异常：{e}")

        self._search_key = ""
        # 保存配置
        self.__update_config()

    def get_state(self) -> bool:
        return self._enabled
    
    @staticmethod
    def exec_shell_command(command) -> str:
        result = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output = result.stdout.strip()
        return output

    @staticmethod
    def re_group1(pattern, s) -> str:
        rs = re.search(pattern, s)
        if rs is None:
            return ''
        return rs.group(1)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/download", 
            "endpoint": self.download, 
            "methods": ["POST"], 
            "summary": "下载指定种子", 
            "description": "搜索种子后下载指定种子", 
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass

    def download(self, torrent_in: schemas.TorrentInfo, current_user: User = Depends(get_current_active_user)):

        # 元数据
        metainfo = MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
        # 媒体信息
        mediainfo = MediaChain().recognize_media(meta=metainfo)
        if not mediainfo:
            if not self._download_path:
                return schemas.Response(
                    success=False,
                    message=f"无法识别媒体信息且下载路径为空"
                )
            else:
                # 虚构媒体信息
                mediainfo = MediaInfo()
                mediainfo.category = "其它"
                mediainfo.type = MediaType.UNKNOWN
                mediainfo.title = metainfo.title

        
        logger.info(f"从【{torrent_in.site_name}】站点下载种子【{torrent_in.title}】【{torrent_in.enclosure}】至【{self._download_path}】")
        
        # 种子信息
        torrentinfo = TorrentInfo()
        torrentinfo.from_dict(torrent_in.dict())
        # 上下文
        context = Context(
            meta_info=metainfo,
            media_info=mediainfo,
            torrent_info=torrentinfo
        )
        try:
            # 如果没有设置下载路径，会自动根据识别的媒体信息设置下载路径
            did = DownloadChain().download_single(
                context=context, 
                username=current_user.name, 
                save_path=self._download_path)
            return schemas.Response(success=True if did else False, data={
                "download_id": did
            })
        except Exception as e:

            logger.error(traceback.format_exc())
            return schemas.Response(
                    success=False,
                    message=f"下载过程中发生异常: {str(e)}"
                ) 

    def search_torrent(self):
        """
        从站点搜索种子
        """
        if not self.sites.get_indexers():
            return

        logger.info("开始搜索站点种子 ...")

        with lock:
            logger.info("清除搜索结果数据")
            self._torrent_data = []
            self.del_data("torrent_search_result")
            self.del_data("torrent_search_key")
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            # 没有指定站点，默认使用全部站点
            if not self._search_sites:
                search_sites = all_sites
            else:
                search_sites = [site for site in all_sites if
                                 site.get("id") in self._search_sites]
            if not search_sites:
                return
            search_site_names = ', '.join([site.get("name") for site in search_sites])
            logger.info(f"开始从{len(search_sites)}个站点搜索数据：{search_site_names}")
            search_site_ids = [site.get('id') for site in search_sites]

            # 搜索多个站点
            self.__search_all_sites(search_site_ids)

            num_torrent = len(self._torrent_data)
            messages = f"种子搜索完成，共搜索到{num_torrent}个种子" if num_torrent > 0 else "没有搜索到种子数据，请更换站点或搜索关键词"
            # logger.info(f"搜索结果：{self._torrent_data}")
            logger.info(messages)
            # 通知搜索完成
            if self._notify:
                self.post_message(mtype=NotificationType.Download,
                                    title="种子搜索结果", text=messages)
            
            # 保存数据
            self.save_data("torrent_search_result", self._torrent_data)
            self.save_data("torrent_search_key", self._search_key)

    def __search_all_sites(self, site_ids):
        for site_id in site_ids:
            self.__search_torrent(site_id)

    def __search_torrent(self, site_id):
        """
        搜索单个site种子信息
        :param site_id: 站点id
        :return:
        """
        i = 0
        while True:
            torrents = SearchChain().search_by_title(self._search_key, i, site_id)
            num_torrents = len(torrents)
            if num_torrents > 0:
                self._torrent_data.extend([t.to_dict().get('torrent_info') for t in torrents])
                
            if num_torrents > 0 and (num_torrents % 10) == 0:
                # 如果当前页返回的种子数目大于0且不能被10整除，那么继续搜索下一页
                # 这个判断基于“各个站点每页返回种子数量为10的整数”这样的假设
                # 基于这个判断，可能仍然会多一次搜索请求，比如：
                # 站点每页返回50个种子，但是当前页面返回10个种子
                i += 1
            else:
                break

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "search_sites": self._search_sites,
            "search_key": self._search_key,
            "download_path": self._download_path
        })

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            search_sites = config.get("search_sites")
            if search_sites:
                if isinstance(search_sites, str):
                    search_sites = [search_sites]

                # 删除对应站点
                if site_id:
                    search_sites = [site for site in search_sites if int(site) != int(site_id)]
                else:
                    # 清空
                    search_sites = []

                # 若无站点，则停止
                if len(search_sites) == 0:
                    self._enabled = False

                self._search_sites = search_sites
                # 保存配置
                self.__update_config()
    
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                         for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])

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
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'search_sites',
                                            'label': '搜索站点',
                                            'items': site_options
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'search_key',
                                            'label': '搜索关键词',
                                            'placeholder': '输入搜索关键词，不可留空'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'download_path',
                                            'label': '下载路径',
                                            'placeholder': '种子下载路径'
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
            "notify": True,
            "search_key": "",
            "search_sites": [],
            "download_path": "",
        }

    def get_page(self) -> List[dict]:
        """
        拼装搜索结果详情页面，需要返回页面配置，同时附带数据
        """
        # 获取保存的数据
        torrents = self.get_data("torrent_search_result")
        keywords = self.get_data("torrent_search_key")

        if (not torrents) or len(torrents) == 0:
            return [
                {
                    'component': 'div',
                    'text': f'没有搜索到种子，请更换搜索关键词：{keywords}' 
                }
            ]
        
        def getVolumeFactorClass(downloadVolume: int, uploadVolume: int):
            if not downloadVolume:
                return 'text-white bg-lime-500'
            if (downloadVolume == 0):
                return 'text-white bg-lime-500'
            elif (downloadVolume < 1):
                return 'text-white bg-green-500'
            elif ((not uploadVolume) or uploadVolume != 1):
                return 'text-white bg-sky-500'
            else:
                return 'text-white bg-gray-500'
        
        def genTitle(torrent):
            contents = []
            contents.append({
                'component': 'div',
                'props': {
                    'class': 'text-high-emphasis pt-1'
                },
                'text': torrent.get("title")
            })

            contents.append({
                'component': 'div',
                'props': {
                    'class': 'text-sm my-1'
                },
                'text': torrent.get("description")
            })

            if torrent.get('hit_and_run'):
                contents.append({
                    'component': 'VChip',
                    'props': {
                        'variant': 'elevated',
                        'size': 'small',
                        'class': 'me-1 mb-1 text-white bg-black'
                    },
                    'text': 'H&R'
                })

            if torrent.get('freedate_diff'):
                contents.append({
                    'component': 'VChip',
                    'props': {
                        'variant': 'elevated',
                        'color': 'secondary',
                        'size': 'small',
                        'class': 'me-1 mb-1'
                    },
                    'text': torrent.get('freedate_diff')
                })
            
            if torrent.get('labels'):
                for label in torrent.get('labels'):
                    contents.append({
                        'component': 'VChip',
                        'props': {
                            'variant': 'elevated',
                            'color': 'primary',
                            'size': 'small',
                            'class': 'me-1 mb-1'
                        },
                        'text': label
                    })
            
            if torrent.get('downloadvolumefactor') != 1 or torrent.get('uploadvolumefactor') != 1:
                contents.append({
                    'component': 'VChip',
                    'props': {
                        'variant': 'elevated',
                        'size': 'small',
                        'class': getVolumeFactorClass(torrent.get('downloadvolumefactor'), torrent.get('uploadvolumefactor'))
                    },
                    'text': torrent.get('volume_factor')
                })
            return contents
        
        # 种子数据明细
        trs = [
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
                        'text': torrent.get('site_name')
                    },
                    {
                        'component': 'td',
                        'content': [{
                            'component': 'a',
                            'props': {
                                'href': 'javascript:void(0)',
                                'torrent-data': f"{json.dumps(torrent)}",
                                'class': 'torrent-title-link'
                            },
                            'content': genTitle(torrent)
                        }]
                    },
                    {
                        'component': 'td',
                        'content': [
                            {
                                'component': 'div',
                                'text': torrent.get('date_elapsed')
                            },
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'text-sm'
                                },
                                'text': torrent.get('pubdate')
                            }
                        ]
                    },
                    {
                        'component': 'td',
                        'content': [
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'text-nowrap whitespace-nowrap'
                                },
                                'text': StringUtils.str_filesize(torrent.get("size"))
                            }
                        ]
                    },
                    {
                        'component': 'td',
                        'content': [
                            {
                                'component': 'div',
                                'text': torrent.get("seeders")
                            }
                        ]
                    },
                    {
                        'component': 'td',
                        'content': [
                            {
                                'component': 'div',
                                'text': torrent.get("peers")
                            }
                        ]
                    },
                    {
                        'component': 'td',
                        'content': [{
                            'component': 'div',
                            'content': [{
                                'component': 'VChip',
                                'props': {
                                    'variant': 'elevated',
                                    'size': 'default',
                                    'class': 'me-1 mb-1 text-white bg-sky-500'
                                },
                                'content': [{
                                    'component': 'a',
                                    'props': {
                                        'href': torrent.get("page_url"),
                                        'target': '_blank'
                                    },
                                    'text': '查看详情'
                                }]
                            },{
                                'component': 'VChip',
                                'props': {
                                    'variant': 'elevated',
                                    'size': 'default',
                                    'class': 'me-1 mb-1 text-white bg-green-500'
                                },
                                'content': [{
                                    'component': 'a',
                                    'props': {
                                        'href': torrent.get("enclosure"),
                                        'target': '_blank'
                                    },
                                    'text': '下载种子'
                                }]
                            }]
                        }]
                    }
                ]
            } for torrent in torrents
        ]
        # 从前端代码文件中查找需要的代码，比如进度条、弹出框、web请求api
        # import api: grep -m 1 -o -E "import.{1,20}index4.js\"" /public/site.js
        # await I.post: grep -o -E -m 1 "await.{0,5}post"  /public/site.js |head -1
        # import index.js: grep -m 1 -o -E "import\{[^;]+\}from\"./index.js\""  /public/site.js
        # =xx.useToast(): grep -m 1 -o -E "=.{1,5}useToast\(\)"  /public/site.js|head -1
        # download: grep -m 1 -o -E "\{.{1,10};.{1,30}download.{200,280}\}\}"  /public/site.js|head -1
        # {oe();try{const A=await P.post("download/add",l);A.success?r.success(`${l==null?void 0:l.site_name} ${l==null?void 0:l.title} 添加下载成功！`):r.error(`${l==null?void 0:l.site_name} ${l==null?void 0:l.title} 添加下载失败：${A.message||"未知错误"}`)}catch(A){console.error(A)}re()}}
        # code_import_api = TorrentSearch.exec_shell_command('grep -m 1 -o -E "import.{1,20}index4.js\\"" /public/site.js')
        code_import_toast = TorrentSearch.exec_shell_command('grep -o -E "import\{[^;]+\}from\\"./index.js\\""  /public/site.js')
        code_use_toast = TorrentSearch.exec_shell_command('grep -m 1 -o -E "=.{1,5}useToast\(\)"  /public/site.js|head -1')
        code_post = TorrentSearch.exec_shell_command('grep -o -E -m 1 "await.{0,5}post"  /public/site.js |head -1')
        code_progress = TorrentSearch.exec_shell_command('grep -m 1 -o -E "\{.{1,10};.{1,30}' + code_post + '.{20,280}\}\}"  /public/site.js|head -1')
        code_progress_start = TorrentSearch.re_group1(self._pattern_progress_start, code_progress)
        code_progress_end = TorrentSearch.re_group1(self._pattern_progress_end, code_progress)
        # # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': f'搜索关键词：{keywords}'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 各站点数据明细
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pt-2',
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True,
                                    'fixed-header': True,
                                    'height': '500px'
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'content': [{
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': '站点'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': '标题'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': '时间'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': '大小'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': '做种'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': '下载'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-start'
                                                    },
                                                    'text': ''
                                                }]
                                            },
                                            
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': trs
                                    }
                                ]
                            }
                        ]
                    },
                    # script
                    {
                        'component': 'script',
                        'props': {
                            'type': 'module',
                            'crossorigin': True
                        },
                        'text': f"""
                            {code_import_toast};
                            const downloadToast {code_use_toast};
                            async function addDownload(torrent) {{
                                {code_progress_start};
                                try {{
                                    
                                    const torrentRs = {code_post}("plugin/TorrentSearch/download?apikey={settings.API_TOKEN}", torrent);
                                    torrentRs.success ? downloadToast.success(`${{torrent == null ? void 0 : torrent.site_name}} ${{torrent == null ? void 0 : torrent.title}} 添加下载成功！`, {{duration: 5000}}) : downloadToast.error(`${{torrent == null ? void 0 : torrent.site_name}} ${{torrent == null ? void 0 : torrent.title}} 添加下载失败：${{torrentRs.message || "未知错误"}}`, {{duration: 5000}})
                                }} catch (Exp) {{
                                    console.error(Exp);
                                }}
                                {code_progress_end};
                            }};
                            var torrentElements = document.getElementsByClassName("torrent-title-link");
 
                            for (var torrentIdx = 0; torrentIdx < torrentElements.length; torrentIdx++) {{
                                var torrentElement = torrentElements[torrentIdx];
                                const torrentData = JSON.parse(torrentElement.getAttribute("torrent-data"));
                                torrentElement.removeAttribute("torrent-data");
                                torrentElement.addEventListener('click', function() {{
                                    addDownload(torrentData);
                                }});
                            }}
                        """
                    },
                    # 自定义样式
                    {
                        'component': 'style',
                        'props': {
                            'type': 'text/css'
                        },
                        'text': """
                            div.v-toast.v-toast--bottom {
                                z-index: 3000;
                            }
                        """
                    }
                ]
            }
        ]
    