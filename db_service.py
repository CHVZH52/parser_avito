import sqlite3

from models import Item


class SQLiteDBHandler:
    """Работа с БД sqlite"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(SQLiteDBHandler, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_name="database.db"):
        if not hasattr(self, "_initialized"):
            self.db_name = db_name
            self._create_table()
            self._initialized = True

    def _create_table(self):
        """Создает таблицу viewed, если она не существует."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS viewed (
                    id INTEGER PRIMARY KEY,
                    price INTEGER
                )
                """
            )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_viewed_id ON viewed(id)"
            )
            conn.commit()

    def add_record(self, ad: Item):
        """Добавляет новую запись в таблицу viewed."""
        value = self._extract_price(ad)
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO viewed (id, price)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET price = excluded.price
                """,
                (ad.id, value),
            )
            conn.commit()

    def add_record_from_page(self, ads: list[Item]):
        """Добавляет несколько записей в таблицу viewed."""
        records = [(ad.id, self._extract_price(ad)) for ad in ads if ad.id]

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO viewed (id, price)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET price = excluded.price
                """,
                records,
            )
            conn.commit()

    def record_exists(self, record_id, price, track_price_changes: bool = True):
        """Проверяет, существует ли запись с заданными параметрами."""
        if record_id is None:
            return False
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            if track_price_changes:
                cursor.execute(
                    "SELECT 1 FROM viewed WHERE id = ? AND price = ?",
                    (record_id, price),
                )
            else:
                cursor.execute(
                    "SELECT 1 FROM viewed WHERE id = ?",
                    (record_id,),
                )
            return cursor.fetchone() is not None

    @staticmethod
    def _extract_price(ad: Item) -> int:
        try:
            return int(ad.priceDetailed.value)
        except Exception:
            return 0
