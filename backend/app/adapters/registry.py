from app.adapters.atb import AtbAdapter
from app.adapters.base import BaseAdapter
from app.adapters.dnipro_m import DniproMAdapter
from app.adapters.epicentrk import EpicentrKAdapter
from app.adapters.fora import ForaAdapter
from app.adapters.fozzy import FozzyAdapter
from app.adapters.metro import MetroAdapter
from app.adapters.monobank import MonobankAdapter
from app.adapters.bars import BarsAdapter
from app.adapters.brsm import BrsmAdapter
from app.adapters.scanner import ScannerAdapter
from app.adapters.silpo import SilpoAdapter
from app.adapters.ukrnafta import UkrnaftaAdapter
from app.adapters.wog import WogAdapter
from app.adapters.upg import UpgAdapter
from app.adapters.foxtrot import FoxtrotAdapter
from app.adapters.klo import KloAdapter
from app.adapters.liki import LikiAdapter
from app.adapters.vencon import VenconAdapter
from app.adapters.prom import PromAdapter
from app.adapters.rozetka import RozetkaAdapter
from app.adapters.aliexpress import AliExpressAdapter
from app.adapters.brain import BrainAdapter
from app.adapters.eva import EvaAdapter
from app.adapters.tabletki import TabletkiAdapter
from app.adapters.olx import OlxAdapter

_ADAPTERS: dict[str, BaseAdapter] = {
    "atb": AtbAdapter(),
    "fozzy": FozzyAdapter(),
    "fora": ForaAdapter(),
    "metro": MetroAdapter(),
    "scanner": ScannerAdapter(),
    "silpo": SilpoAdapter(),
    "epicentrk": EpicentrKAdapter(),
    "wog": WogAdapter(),
    "ukrnafta": UkrnaftaAdapter(),
    "bars": BarsAdapter(),
    "brsm": BrsmAdapter(),
    "dnipro-m": DniproMAdapter(),
    "upg": UpgAdapter(),
    "foxtrot": FoxtrotAdapter(),
    "klo": KloAdapter(),
    "vencon": VenconAdapter(),
    "prom": PromAdapter(),
    "rozetka": RozetkaAdapter(),
    "aliexpress": AliExpressAdapter(),
    "brain": BrainAdapter(),
    "eva": EvaAdapter(),
    "liki": LikiAdapter(),
    "tabletki": TabletkiAdapter(),
    "olx": OlxAdapter(),
    "monobank": MonobankAdapter(),
}


def get_adapter(store_code: str) -> BaseAdapter:
    try:
        return _ADAPTERS[store_code]
    except KeyError:
        raise ValueError(f"Unknown store code: {store_code}")
