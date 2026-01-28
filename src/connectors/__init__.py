import pymysql
import os
from dotenv import load_dotenv

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_SCHEME = os.getenv("DB_SCHEME")


class Connector:
    def __init__(self):
        self.conn = None
        self.curs = None

    def __enter__(self):
        self.conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_SCHEME,
            port=int(DB_PORT),
        )
        self.curs = self.conn.cursor(pymysql.cursors.DictCursor)
        return self  # with 블록의 as 변수에 들어감

    def cursor(self):
        return self.curs

    def __exit__(self, exc_type, exc_value, traceback):

        if self.curs:
            self.curs.close()
        if exc_type:
            print(f"Exception occurred: {exc_type}, {exc_value}")
        else:
            self.conn.commit()
        self.conn.close()
        return False
