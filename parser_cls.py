import asyncio
from copy import deepcopy
import html
import json
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests
import os
from loguru import logger
from pydantic import ValidationError
from requests.cookies import RequestsCookieJar

from common_data import HEADERS
from db_service import SQLiteDBHandler
from dto import Proxy, AvitoConfig, SearchQuery
from get_cookies import get_cookies
from hide_private_data import log_config
from load_config import load_avito_config
from user_filters import UserFiltersStorage
from models import ItemsResponse, Item
from tg_sender import SendAdToTg
from version import VERSION
from xlsx_service import XLSXHandler
from paths_helper import user_xlsx_path, user_cookies_path

DEBUG_MODE = False

REGION_URL_MAP = {
    "all": "all",
    "moscow": "moskva",
    "moscow_mo": "moskva_i_mo",
    "mo": "moskovskaya_oblast",
    "moscow_region": "moskovskaya_oblast",
}

REGION_SLUG_TO_KEY = {slug: key for key, slug in REGION_URL_MAP.items()}

REGION_LABELS = {
    "all": "Все регионы",
    "moscow": "Москва",
    "moscow_mo": "Москва и МО",
    "mo": "Московская область",
    "moscow_region": "Московская область",
}

def _configure_logging() -> None:
    def _try_add(path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.add(path, rotation="5 MB", retention="5 days", level="DEBUG")
            return True
        except PermissionError:
            return False

    project_log = Path(__file__).resolve().parent / "logs" / "app.log"
    if _try_add(project_log):
        return

    fallback_log = Path(tempfile.gettempdir()) / "avito_parser_logs" / "app.log"
    if _try_add(fallback_log):
        logger.info(f"Логи сохраняются в {fallback_log} из-за ограничений доступа")
        return

    logger.add(sys.stderr, level="DEBUG")
    logger.warning("Не удалось создать файл журнала — пишем в stdout")

_configure_logging()


class AvitoParse:
    def __init__(
            self,
            config: AvitoConfig,
            stop_event=None
    ):
        self.config = config
        self.proxy_obj = self.get_proxy_obj()
        self.active_search = None
        self.result_dir = self._ensure_result_dir()
        self.chat_owner = getattr(self.config, "chat_owner", None) or self._resolve_chat_owner()
        self.filter_title = getattr(self.config, "filter_title", None)
        self.filter_interval_seconds = getattr(self.config, "filter_interval_seconds", None)
        self.skip_initial_notifications = getattr(self.config, "skip_first_notifications", False)
        self.export_user_id = getattr(self.config, "export_user_id", None)
        self.filters_storage = UserFiltersStorage()
        self.filters_storage = UserFiltersStorage()
        self.cookies_file = self._resolve_cookies_path()
        self.db_path = self._resolve_db_path()
        self.initial_summary_sent = getattr(self.config, "initial_summary_sent", False)
        self.initial_batch_mode = (
            not self.skip_initial_notifications
            and not self.initial_summary_sent
            and self.chat_owner not in {None, "global"}
        )
        self.initial_batch_buffer: list[Item] = []
        self.db_handler = SQLiteDBHandler(db_name=str(self.db_path))
        self.tg_handler = self.get_tg_handler()
        self.xlsx_handler = XLSXHandler(self.__get_file_title())
        self.stop_event = stop_event
        self.cookies = None
        self.session = requests.Session()
        self.headers = HEADERS
        self.good_request_count = 0
        self.bad_request_count = 0
        self.notifications_ready = self._initial_notifications_ready()
        self._initial_skip_logged = False

        log_config(config=self.config, version=VERSION)

    def get_tg_handler(self) -> SendAdToTg | None:
        if all([self.config.tg_token, self.config.tg_chat_id]):
            return SendAdToTg(bot_token=self.config.tg_token, chat_id=self.config.tg_chat_id)
        return None

    def _send_to_tg(self, ads: list[Item]) -> None:
        if not self.tg_handler:
            return
        if self.initial_batch_mode:
            self.initial_batch_buffer.extend(ads)
            return
        if not self.notifications_ready:
            if not self._initial_skip_logged:
                logger.info("Пропускаю уведомления для первого запуска (%s)", self.chat_owner)
                self._initial_skip_logged = True
            return
        for ad in ads:
            self._annotate_ad(ad)
            self.tg_handler.send_to_tg(ad=ad)

    def _annotate_ad(self, ad: Item) -> None:
        if hasattr(ad, "filter_title"):
            ad.filter_title = self.filter_title or (self.active_search.text if self.active_search else None)
        if hasattr(ad, "filter_interval_seconds"):
            ad.filter_interval_seconds = self.filter_interval_seconds
        if hasattr(ad, "filter_region_label"):
            ad.filter_region_label = self._current_region_label()

    def _send_initial_batch_summary(self, ads: list[Item]) -> None:
        if not ads:
            return
        chunk_size = 5
        total = len(ads)
        chunks = [ads[i:i + chunk_size] for i in range(0, len(ads), chunk_size)]
        title = self.filter_title or (self.active_search.text if self.active_search else "Запрос")
        for idx, chunk in enumerate(chunks, 1):
            lines = [
                f"✨ Стартовый пакет {idx}/{len(chunks)} для запроса {SendAdToTg._escape(title)} "
                f"({len(chunk)} из {total})"
            ]
            for offset, ad in enumerate(chunk, 1):
                title_text = SendAdToTg._escape(ad.title or "Без названия")
                price_value = getattr(getattr(ad, "priceDetailed", None), "value", 0)
                price_text = SendAdToTg._format_price(price_value)
                full_url = f"https://www.avito.ru/{ad.urlPath}" if ad.urlPath else f"https://www.avito.ru/{ad.id}"
                number = (idx - 1) * chunk_size + offset
                lines.append(f"{number}. [{title_text}]({full_url}) — {price_text} ₽")
            lines.append("Дальше я буду присылать объявления по одному с фото и ссылкой.")
            self.tg_handler.send_to_tg(msg="\n".join(lines))
        self.initial_summary_sent = True
        chat_value = self.export_user_id
        try:
            if chat_value and self.config.filter_id:
                self.filters_storage.mark_initial_summary_sent(int(chat_value), self.config.filter_id)
        except Exception as err:
            logger.debug("Не удалось отметить отправку стартового пакета: %s", err)

    def get_proxy_obj(self) -> Proxy | None:
        if all([self.config.proxy_string, self.config.proxy_change_url]):
            return Proxy(
                proxy_string=self.config.proxy_string,
                change_ip_link=self.config.proxy_change_url
            )
        logger.info("Работаем без прокси")
        return None

    def get_cookies(self, max_retries: int = 1, delay: float = 2.0) -> dict | None:
        for attempt in range(1, max_retries + 1):
            if self.stop_event and self.stop_event.is_set():
                return None

            try:
                cookies, user_agent = asyncio.run(
                    get_cookies(proxy=self.proxy_obj, headless=True, stop_event=self.stop_event))
                if cookies:
                    logger.info(f"[get_cookies] Успешно получены cookies с попытки {attempt}")

                    self.headers["user-agent"] = user_agent
                    return cookies
                else:
                    raise ValueError("Пустой результат cookies")
            except Exception as e:
                logger.warning(f"[get_cookies] Попытка {attempt} не удалась: {e}")
                if attempt < max_retries:
                    time.sleep(delay * attempt)  # увеличиваем задержку
                else:
                    logger.error(f"[get_cookies] Все {max_retries} попытки не удались")
                    return None

    def save_cookies(self) -> None:
        """Сохраняет cookies из requests.Session в JSON-файл."""
        try:
            with self.cookies_file.open("w", encoding="utf-8") as f:
                json.dump(self.session.cookies.get_dict(), f)
        except PermissionError:
            fallback = Path(tempfile.gettempdir()) / "avito_parser_cookies.json"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            with fallback.open("w", encoding="utf-8") as f:
                json.dump(self.session.cookies.get_dict(), f)

    def load_cookies(self) -> None:
        """Загружает cookies из JSON-файла в requests.Session."""
        try:
            with self.cookies_file.open("r", encoding="utf-8") as f:
                cookies = json.load(f)
        except (FileNotFoundError, PermissionError):
            return
        jar = RequestsCookieJar()
        for k, v in cookies.items():
            jar.set(k, v)
        self.session.cookies.update(jar)

    def fetch_data(self, url, retries=3, backoff_factor=1):
        proxy_data = None
        if self.proxy_obj:
            proxy_data = {
                          "https": f"http://{self.config.proxy_string}"
            }

        for attempt in range(1, retries + 1):
            if self.stop_event and self.stop_event.is_set():
                return

            try:
                response = self.session.get(
                    url=url,
                    headers=self.headers,
                    proxies=proxy_data,
                    cookies=self.cookies,
                    impersonate="chrome",
                    timeout=20,
                    verify=False,
                )
                logger.debug(f"Попытка {attempt}: {response.status_code}")

                if response.status_code >= 500:
                    raise requests.RequestsError(f"Ошибка сервера: {response.status_code}")
                if response.status_code == 429:
                    self.bad_request_count += 1
                    self.session = requests.Session()
                    self.change_ip()
                    self.cookies = self.get_cookies()
                    raise requests.RequestsError(f"Слишком много запросов: {response.status_code}")
                if response.status_code in [403, 302]:
                    self.cookies = self.get_cookies()
                    raise requests.RequestsError(f"Заблокирован: {response.status_code}")

                self.save_cookies()
                self.good_request_count += 1
                return response.text
            except requests.RequestsError as e:
                logger.debug(f"Попытка {attempt} закончилась неуспешно: {e}")
                if attempt < retries:
                    sleep_time = backoff_factor * attempt
                    logger.debug(f"Повтор через {sleep_time} секунд...")
                    time.sleep(sleep_time)
                else:
                    logger.info("Все попытки были неуспешными")
                    return None

    def parse(self):
        if self.config.one_file_for_link:
            self.xlsx_handler = None
        resolved_targets = self._resolve_input_links()

        for _index, (search_meta, url) in enumerate(resolved_targets):
            self.active_search = search_meta
            logger.info(f"Старт обработки: {url}")
            ads_in_link = []
            for i in range(0, self.config.count):
                if self.stop_event and self.stop_event.is_set():
                    return
                if DEBUG_MODE:
                    html_code = open("response.txt", "r", encoding="utf-8").read()
                else:
                    html_code = self.fetch_data(url=url, retries=self.config.max_count_of_retry)

                if not html_code:
                    logger.warning(
                        f"Не удалось получить HTML для {url}, пробую заново через {self.config.pause_between_links} сек.")
                    time.sleep(self.config.pause_between_links)
                    continue

                if not self.xlsx_handler and self.config.one_file_for_link:
                    self.xlsx_handler = XLSXHandler(self._single_file_path(_index, search_meta))

                data_from_page = self.find_json_on_page(html_code=html_code)
                try:
                    catalog = data_from_page.get("data", {}).get("catalog") or {}
                    ads_models = ItemsResponse(**catalog)
                except ValidationError as err:
                    logger.error(f"При валидации объявлений произошла ошибка: {err}")
                    continue

                ads = self._clean_null_ads(ads=ads_models.items)

                ads = self._add_seller_to_ads(ads=ads)

                if not ads:
                    logger.info("Объявления закончились, заканчиваю работу с данной ссылкой")
                    break

                filter_ads = self.filter_ads(ads=ads)

                if self.tg_handler and not self.config.one_time_start:
                    self._send_to_tg(ads=filter_ads)

                filter_ads = self.parse_views(ads=filter_ads)

                if filter_ads:
                    self.__save_viewed(ads=filter_ads, chat_owner=self.chat_owner)

                    if self.config.save_xlsx:
                        ads_in_link.extend(filter_ads)

                url = self.get_next_page_url(url=url)
                if url:
                    logger.info(f"Следующая страница: {url}")

                logger.info(f"Пауза {self.config.pause_between_links} сек.")
                time.sleep(self.config.pause_between_links)

            if ads_in_link:
                logger.info(f"Сохраняю в Excel {len(ads_in_link)} объявлений")
                self.__save_data(ads=ads_in_link)
            else:
                logger.info("Сохранять нечего")

            if self.initial_batch_mode and self.initial_batch_buffer:
                self._send_initial_batch_summary(self.initial_batch_buffer)
                self.initial_batch_buffer.clear()
                self.initial_batch_mode = False

            if self.config.one_file_for_link:
                self.xlsx_handler = None
            self.active_search = None

        logger.info(f"Хорошие запросы: {self.good_request_count}шт, плохие: {self.bad_request_count}шт")

        if self.config.one_time_start and self.tg_handler:
            self.tg_handler.send_to_tg(msg="Парсинг Авито завершён. Все ссылки обработаны")
            self.stop_event = True

    @staticmethod
    def _clean_null_ads(ads: list[Item]) -> list[Item]:
        return [ad for ad in ads if ad.id]

    @staticmethod
    def find_json_on_page(html_code, data_type: str = "mime") -> dict:
        soup = BeautifulSoup(html_code, "html.parser")
        try:
            for _script in soup.select('script'):
                script_type = _script.get('type')

                if data_type == 'mime' and script_type == 'mime/invalid':
                    script_content = html.unescape(_script.text)
                    parsed_data = json.loads(script_content)

                    if 'state' in parsed_data:
                        return parsed_data['state']

                    elif 'data' in parsed_data:
                        logger.info("data")
                        return parsed_data['data']

                    else:
                        return parsed_data

        except Exception as err:
            logger.error(f"Ошибка при поиске информации на странице: {err}")
        return {}

    def filter_ads(self, ads: list[Item]) -> list[Item]:
        """Сортирует объявления"""
        filters = [
            self._filter_viewed,
            self._filter_by_price_range,
            self._filter_by_black_keywords,
            self._filter_by_white_keyword,
            self._filter_by_address,
            self._filter_by_delivery,
            self._filter_by_seller,
            self._filter_by_recent_time,
            self._filter_by_reserve,
            self._filter_by_promotion,
        ]

        for filter_fn in filters:
            ads = filter_fn(ads)
            logger.info(f"После фильтрации {filter_fn.__name__} осталось {len(ads)}")
            if not len(ads):
                return ads
        return ads

    def _filter_by_price_range(self, ads: list[Item]) -> list[Item]:
        min_price, max_price = self._get_active_price_bounds()
        if min_price is None and max_price is None:
            return ads
        filtered = []
        for ad in ads:
            try:
                price_value = ad.priceDetailed.value
            except Exception:
                continue
            if min_price is not None and price_value < min_price:
                continue
            if max_price is not None and price_value > max_price:
                continue
            filtered.append(ad)
        return filtered

    def _filter_by_black_keywords(self, ads: list[Item]) -> list[Item]:
        if not self.config.keys_word_black_list:
            return ads
        try:
            return [ad for ad in ads if not self._is_phrase_in_ads(ad=ad, phrases=self.config.keys_word_black_list)]
        except Exception as err:
            logger.debug(f"Ошибка при проверке объявлений по списку стоп-слов: {err}")
            return ads

    def _filter_by_white_keyword(self, ads: list[Item]) -> list[Item]:
        if not self.config.keys_word_white_list:
            return ads
        try:
            return [ad for ad in ads if self._is_phrase_in_ads(ad=ad, phrases=self.config.keys_word_white_list)]
        except Exception as err:
            logger.debug(f"Ошибка при проверке объявлений по списку обязательных слов: {err}")
            return ads

    def _filter_by_address(self, ads: list[Item]) -> list[Item]:
        if not self.config.geo:
            return ads
        try:
            return [ad for ad in ads if self.config.geo in ad.geo.formattedAddress]
        except Exception as err:
            logger.debug(f"Ошибка при проверке объявлений по адресу: {err}")
            return ads

    def _filter_by_delivery(self, ads: list[Item]) -> list[Item]:
        mode = self._active_delivery_mode()
        if mode == "any":
            return ads
        try:
            if mode == "delivery_only":
                return [ad for ad in ads if ad.contacts and ad.contacts.delivery]
            return [ad for ad in ads if not (ad.contacts and ad.contacts.delivery)]
        except Exception as err:
            logger.debug(f"Ошибка при фильтрации по доставке: {err}")
            return ads

    def _filter_viewed(self, ads: list[Item]) -> list[Item]:
        track_price_changes = self._should_track_price_changes()
        try:
            return [ad for ad in ads if not self.is_viewed(ad=ad, track_price_changes=track_price_changes)]
        except Exception as err:
            logger.debug(f"Ошибка при проверке объявления по признаку смотрели или не смотрели: {err}")
            return ads

    def _add_seller_to_ads(self, ads: list[Item]) -> list[Item]:
        for ad in ads:
            if seller_id := self._extract_seller_slug(data=ad):
                ad.sellerId = seller_id
        return ads

    @staticmethod
    def _add_promotion_to_ads(ads: list[Item]) -> list[Item]:
        for ad in ads:
            ad.isPromotion = any(
                v.get("title") == "Продвинуто"
                for step in (ad.iva or {}).get("DateInfoStep", [])
                for v in step.payload.get("vas", [])
            )
        return ads

    def _filter_by_seller(self, ads: list[Item]) -> list[Item]:
        if not self.config.seller_black_list:
            return ads
        try:
            return [ad for ad in ads if not ad.sellerId or ad.sellerId not in self.config.seller_black_list]
        except Exception as err:
            logger.debug(f"Ошибка при отсеивании объявления с продавцами из черного списка : {err}")
            return ads

    def _filter_by_recent_time(self, ads: list[Item]) -> list[Item]:
        if not self.config.max_age:
            return ads
        try:
            return [ad for ad in ads if
                    self._is_recent(timestamp_ms=ad.sortTimeStamp, max_age_seconds=self.config.max_age)]
        except Exception as err:
            logger.debug(f"Ошибка при отсеивании слишком старых объявлений: {err}")
            return ads

    def _filter_by_reserve(self, ads: list[Item]) -> list[Item]:
        if not self.config.ignore_reserv:
            return ads
        try:
            return [ad for ad in ads if not ad.isReserved]
        except Exception as err:
            logger.debug(f"Ошибка при отсеивании объявлений в резерве: {err}")
            return ads

    def _filter_by_promotion(self, ads: list[Item]) -> list[Item]:
        ads = self._add_promotion_to_ads(ads=ads)
        if not self.config.ignore_promotion:
            return ads
        try:
            return [ad for ad in ads if not ad.isPromotion]
        except Exception as err:
            logger.debug(f"Ошибка при отсеивании продвинутых объявлений: {err}")
            return ads

    def parse_views(self, ads: list[Item]) -> list[Item]:
        if not self.config.parse_views:
            return ads

        logger.info("Начинаю парсинг просмотров")

        for ad in ads:
            try:
                html_code_full_page = self.fetch_data(url=f"https://www.avito.ru{ad.urlPath}")
                ad.total_views, ad.today_views = self._extract_views(html=html_code_full_page)
                delay = random.uniform(0.1, 0.9)
                time.sleep(delay)
            except Exception as err:
                logger.warning(f"Ошибка при парсинге {ad.urlPath}: {err}")
                continue

        return ads

    @staticmethod
    def _extract_views(html: str) -> tuple:
        soup = BeautifulSoup(html, "html.parser")

        def extract_digits(element):
            return int(''.join(filter(str.isdigit, element.get_text()))) if element else None

        total = extract_digits(soup.select_one('[data-marker="item-view/total-views"]'))
        today = extract_digits(soup.select_one('[data-marker="item-view/today-views"]'))

        return total, today

    def change_ip(self) -> bool:
        if not self.config.proxy_change_url:
            logger.info("Сейчас бы была смена ip, но мы без прокси")
            return False
        logger.info("Меняю IP")
        try:
            res = requests.get(url=self.config.proxy_change_url, verify=False)
            if res.status_code == 200:
                logger.info("IP изменен")
                return True
        except Exception as err:
            logger.info(f"При смене ip возникла ошибка: {err}")
        logger.info("Не удалось изменить IP, пробую еще раз")
        time.sleep(random.randint(3, 10))
        return self.change_ip()

    @staticmethod
    def _extract_seller_slug(data):
        match = re.search(r"/brands/([^/?#]+)", str(data))
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _is_phrase_in_ads(ad: Item, phrases: list) -> bool:
        full_text_from_ad = (ad.title + ad.description).lower()
        return any(phrase.lower() in full_text_from_ad for phrase in phrases)

    def is_viewed(self, ad: Item, track_price_changes: bool = True) -> bool:
        """Проверяет, смотрели мы это или нет"""
        try:
            price_value = int(getattr(getattr(ad, "priceDetailed", None), "value", 0))
        except Exception:
            price_value = 0
        previous_price = self.db_handler.get_price(record_id=ad.id, chat_id=self.chat_owner)
        if previous_price is None:
            return False
        if track_price_changes and previous_price != price_value:
            if hasattr(ad, "price_change_from"):
                ad.price_change_from = previous_price
            return False
        return True

    @staticmethod
    def _is_recent(timestamp_ms: int, max_age_seconds: int) -> bool:
        now = datetime.utcnow()
        published_time = datetime.utcfromtimestamp(timestamp_ms / 1000)
        return (now - published_time) <= timedelta(seconds=max_age_seconds)

    def __get_file_title(self) -> str:
        """Определяет название файла"""
        user_id = self.export_user_id or self._primary_user_chat_id()
        if user_id:
            user_path = user_xlsx_path(user_id, base_dir=self.result_dir)
            user_path.parent.mkdir(parents=True, exist_ok=True)
            return str(user_path)

        title_file = 'all'
        if getattr(self.config, "searches", None):
            parts = [self._slugify(search.text, fallback=f"query-{idx + 1}") for idx, search in enumerate(self.config.searches)]
            title_file = "-".join(parts)[:50] or "searches"
        elif getattr(self.config, "queries", None):
            title_file = "-".join(self._slugify(q) for q in self.config.queries)[:50]
        elif self.config.keys_word_white_list:
            title_file = "-".join(self._slugify(word) for word in self.config.keys_word_white_list)[:50]

        return str(self.result_dir / f"{title_file}.xlsx")

    def _ensure_result_dir(self) -> Path:
        default = Path(__file__).resolve().parent / "result"
        try:
            default.mkdir(parents=True, exist_ok=True)
            if not os.access(default, os.W_OK | os.X_OK):
                raise PermissionError("Нет прав на запись/вход в result/")
            return default
        except PermissionError as err:
            fallback = Path(tempfile.gettempdir()) / "avito_parser_result"
            fallback.mkdir(parents=True, exist_ok=True)
            logger.info(f"{err} — сохраняем в {fallback}")
            return fallback

    def _resolve_db_path(self) -> Path:
        default = Path(__file__).resolve().parent / "database.db"
        try:
            default.touch(exist_ok=True)
            if not os.access(default, os.W_OK | os.R_OK):
                raise PermissionError("Нет прав на запись в database.db")
            return default
        except PermissionError as err:
            fallback = Path(tempfile.gettempdir()) / "avito_parser_database.db"
            fallback.touch(exist_ok=True)
            logger.info(f"{err} — используем {fallback}")
            return fallback

    def _resolve_chat_owner(self) -> str:
        chats = self.config.tg_chat_id or []
        if len(chats) == 1:
            return str(chats[0])
        return "global"

    def _initial_notifications_ready(self) -> bool:
        if not self.skip_initial_notifications:
            return True
        return self._has_history()

    def _has_history(self) -> bool:
        chat_owner = getattr(self, "chat_owner", None)
        if not chat_owner or chat_owner == "global":
            return True
        try:
            return self.db_handler.has_history(chat_owner)
        except Exception:
            return True

    def _primary_user_chat_id(self) -> str | None:
        chats = self.config.tg_chat_id or []
        if chats:
            return str(chats[0])
        return None

    def _resolve_cookies_path(self) -> Path:
        owner = getattr(self, "chat_owner", None)
        base_dir = Path(__file__).resolve().parent
        if owner and owner != "global":
            path = user_cookies_path(owner)
        else:
            path = base_dir / "cookies.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            return path
        except PermissionError:
            fallback_base = Path(tempfile.gettempdir()) / "avito_parser_cookies"
            fallback_base.mkdir(parents=True, exist_ok=True)
            if owner and owner != "global":
                fallback = user_cookies_path(owner, base_dir=fallback_base)
            else:
                fallback = fallback_base / "cookies.json"
            fallback.touch(exist_ok=True)
            logger.info(f"Используем {fallback} для cookies из-за прав доступа")
            return fallback

    def _single_file_path(self, index: int, search_meta: SearchQuery | None) -> str:
        name = self._slugify(search_meta.text) if search_meta else f"link-{index + 1}"
        return str(self.result_dir / f"{name}.xlsx")

    @staticmethod
    def _slugify(value: str | None, fallback: str = "query") -> str:
        if not value:
            return fallback
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        slug = slug or fallback
        return slug[:50]

    def _get_active_price_bounds(self) -> tuple[Optional[int], Optional[int]]:
        min_price = None
        max_price = None
        if self.active_search:
            if self.active_search.min_price is not None:
                min_price = self.active_search.min_price
            if self.active_search.max_price is not None:
                max_price = self.active_search.max_price
        if min_price is None and self.config.min_price:
            min_price = self.config.min_price
        if max_price is None and self.config.max_price and self.config.max_price < 999_999_999:
            max_price = self.config.max_price
        return min_price, max_price

    def _current_region_label(self) -> str:
        if self.active_search and getattr(self.active_search, "region", None):
            key = self.active_search.region
        else:
            base_slug = getattr(self.config, "region_slug", None)
            if base_slug:
                key = REGION_SLUG_TO_KEY.get(base_slug, "all")
            else:
                key = "all"
        return REGION_LABELS.get(key, "Все регионы")

    def _active_delivery_mode(self) -> str:
        if self.active_search and self.active_search.delivery:
            return self.active_search.delivery
        if self.config.delivery_only:
            return "delivery_only"
        return "any"

    def _should_track_price_changes(self) -> bool:
        if self.active_search and self.active_search.track_price_changes is not None:
            return self.active_search.track_price_changes
        return True

    def __save_data(self, ads: list[Item]) -> None:
        """Сохраняет результат в файл keyword*.xlsx и в БД"""
        try:
            self.xlsx_handler.append_data_from_page(ads=ads)
        except Exception as err:
            logger.info(f"При сохранении в Excel ошибка {err}")

    def __save_viewed(self, ads: list[Item], chat_owner: str) -> None:
        """Сохраняет просмотренные объявления"""
        try:
            self.db_handler.add_record_from_page(ads=ads, chat_id=chat_owner)
            if self.skip_initial_notifications and not self.notifications_ready and chat_owner not in {None, "global"}:
                self.notifications_ready = True
            if self.initial_batch_mode:
                self.initial_batch_mode = False
        except Exception as err:
            logger.info(f"При сохранении в БД ошибка {err}")

    def get_next_page_url(self, url: str):
        """Получает следующую страницу"""
        try:
            url_parts = urlparse(url)
            query_params = parse_qs(url_parts.query)
            current_page = int(query_params.get('p', [1])[0])
            query_params['p'] = current_page + 1
            if self.config.one_time_start:
                logger.debug(f"Страница {current_page}")

            new_query = urlencode(query_params, doseq=True)
            next_url = urlunparse((url_parts.scheme, url_parts.netloc, url_parts.path, url_parts.params, new_query,
                                   url_parts.fragment))
            return next_url
        except Exception as err:
            logger.error(f"Не смог сформировать ссылку на следующую страницу для {url}. Ошибка: {err}")

    # формирования ссылок по запросу 
    def _resolve_input_links(self) -> list[tuple[SearchQuery | None, str]]:
        links: list[tuple[SearchQuery | None, str]] = []
        searches = getattr(self.config, "searches", None) or []
        if searches:
            for search in searches:
                search_url = self._build_search_url(search)
                logger.info(f"Сформирована поисковая ссылка для запроса '{search.text}': {search_url}")
                links.append((search, search_url))
            return links

        queries = getattr(self.config, "queries", None) or []
        for q in queries:
            q = (q or "").strip()
            if not q:
                continue
            search_stub = SearchQuery(text=q)
            search_url = self._build_search_url(search_stub)
            logger.info(f"Сформирована поисковая ссылка для запроса '{q}': {search_url}")
            links.append((search_stub, search_url))

        if links:
            return links

        return [(None, url) for url in (self.config.urls or [])]

    def _build_search_url(self, search: SearchQuery) -> str:
        region_key = getattr(search, "region", "all") or "all"
        region_slug = REGION_URL_MAP.get(region_key, region_key) or "all"
        base = f"https://www.avito.ru/{region_slug}"

        params_items: list[tuple[str, str | int]] = [("cd", 1), ("q", search.text.lower())]

        min_price = search.min_price if search.min_price is not None else (self.config.min_price or None)
        max_price = search.max_price if search.max_price is not None else (
            self.config.max_price if self.config.max_price and self.config.max_price < 999_999_999 else None
        )
        if min_price is not None:
            params_items.append(("pmin", min_price))
        if max_price is not None:
            params_items.append(("pmax", max_price))

        if search.delivery == "delivery_only":
            params_items.append(("d", 1))

        sort_flag = search.sort_new
        if sort_flag is None:
            sort_flag = self.config.sort_new
        if sort_flag:
            params_items.append(("s", 104))

        query_str = urlencode(params_items, doseq=True)
        return f"{base}?{query_str}"


def build_user_configs(base_config: AvitoConfig, storage: UserFiltersStorage) -> list[AvitoConfig]:
    user_map = storage.get_all_searches()
    if not user_map:
        return [base_config]

    configs: list[AvitoConfig] = []
    for chat_id, searches in user_map.items():
        if not searches:
            continue
        cfg = deepcopy(base_config)
        cfg.searches = searches
        cfg.queries = [search.text for search in searches]
        cfg.tg_chat_id = [str(chat_id)]
        cfg.chat_owner = str(chat_id)
        configs.append(cfg)

    if not configs:
        configs.append(base_config)
    return configs


if __name__ == "__main__":
    try:
        config = load_avito_config("config.toml")
    except Exception as err:
        logger.error(f"Ошибка загрузки конфига: {err}")
        exit(1)

    filters_storage = UserFiltersStorage()

    while True:
        try:
            configs_to_run = build_user_configs(config, filters_storage)
            for cfg in configs_to_run:
                parser = AvitoParse(cfg)
                parser.parse()
            if config.one_time_start:
                logger.info("Парсинг завершен т.к. включён one_time_start в настройках")
                break
            logger.info(f"Парсинг завершен. Пауза {config.pause_general} сек")
            time.sleep(config.pause_general)
        except Exception as err:
            logger.error(f"Произошла ошибка {err}. Будет повторный запуск через 30 сек.")
            time.sleep(30)
