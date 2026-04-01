"""
crawl_to_supabase.py — download store prices and push to Supabase
Usage: python crawl_to_supabase.py --store lidl
       python crawl_to_supabase.py --all
"""
import sys, os, datetime, argparse, requests, logging
sys.path.insert(0, os.path.expanduser('~/cijene-api'))
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/botapp/backend/.env'))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY","")

def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=ignore-duplicates,return=minimal",
        "on_conflict": "store,product,valid_from"
    }

def upsert(records):
    for i in range(0, len(records), 500):
        batch = records[i:i+500]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/products", headers=headers(), json=batch)
        if r.status_code not in (200,201):
            logger.error(f"Upsert error: {r.status_code} {r.text[:200]}")
        else:
            logger.info(f"Upserted {len(batch)} records")

def crawl_store(name, crawler_class, date):
    logger.info(f"Crawling {name}...")
    c = crawler_class()
    stores = c.get_all_products(date)
    if not stores:
        logger.warning(f"No data for {name} today, trying yesterday...")
        yesterday = date - __import__('datetime').timedelta(days=1)
        stores = c.get_all_products(yesterday)
    if not stores:
        logger.warning(f"No data for {name}")
        return 0

    records = []
    today = str(date)
    for store in stores:
        for p in store.items:
            price = float(p.special_price or p.price or 0)
            original = float(p.price or 0)
            is_sale = p.special_price is not None
            records.append({
                "store": name,
                "product": p.product,
                "brand": p.brand or None,
                "quantity": str(p.quantity) + " " + (p.unit or "") if p.quantity else None,
                "sale_price": str(price),
                "original_price": str(original) if is_sale else None,
                "discount_percent": None,
                "category": p.category or "Other",
                "valid_from": today,
                "valid_until": today,
                "is_expired": False,
                "catalogue_name": f"{name}_{today}",
                "catalogue_week": datetime.date.today().strftime("%Y-W%V"),
                "barcode": p.barcode or None,
            })

    upsert(records)
    logger.info(f"{name}: {len(records)} products from {len(stores)} stores")
    return len(records)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=str)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--yesterday", action="store_true")
    args = parser.parse_args()

    date = datetime.date.today() - datetime.timedelta(days=1) if args.yesterday else datetime.date.today()

    from crawler.store.lidl import LidlCrawler
    from crawler.store.konzum import KonzumCrawler
    from crawler.store.spar import SparCrawler
    from crawler.store.kaufland import KauflandCrawler
    from crawler.store.studenac import StudenacCrawler
    from crawler.store.tommy import TommyCrawler
    from crawler.store.plodine import PlodineCrawler
    from crawler.store.eurospin import EurospinCrawler
    from crawler.store.dm import DmCrawler
    from crawler.store.ktc import KtcCrawler
    from crawler.store.metro import MetroCrawler
    from crawler.store.ntl import NtlCrawler
    from crawler.store.ribola import RibolaCrawler
    from crawler.store.roto import RotoCrawler
    from crawler.store.trgocentar import TrgocentarCrawler
    from crawler.store.trgovina_krk import TrgovinaKrkCrawler
    from crawler.store.brodokomerc import BrodokomercCrawler
    from crawler.store.lorenco import LorencoCrawler
    from crawler.store.boso import BosoCrawler
    from crawler.store.vrutak import VrutakCrawler
    from crawler.store.jadranka_trgovina import JadrankaTrgovinaCrawler
    from crawler.store.zabac import ZabacCrawler

    STORES = {
        "lidl": LidlCrawler,
        "konzum": KonzumCrawler,
        "spar": SparCrawler,
        "kaufland": KauflandCrawler,
        "studenac": StudenacCrawler,
        "tommy": TommyCrawler,
        "plodine": PlodineCrawler,
        "eurospin": EurospinCrawler,
        "dm": DmCrawler,
        "ktc": KtcCrawler,
        "metro": MetroCrawler,
        "ntl": NtlCrawler,
        "ribola": RibolaCrawler,
        "roto": RotoCrawler,
        "trgocentar": TrgocentarCrawler,
        "trgovina_krk": TrgovinaKrkCrawler,
        "brodokomerc": BrodokomercCrawler,
        "lorenco": LorencoCrawler,
        "boso": BosoCrawler,
        "vrutak": VrutakCrawler,
        "jadranka_trgovina": JadrankaTrgovinaCrawler,
        "zabac": ZabacCrawler,
    }

    if args.all:
        total = 0
        for name, cls in STORES.items():
            try:
                total += crawl_store(name, cls, date)
            except Exception as e:
                logger.error(f"{name} failed: {e}")
        logger.info(f"Total: {total} products")
    elif args.store:
        cls = STORES.get(args.store)
        if not cls:
            print(f"Unknown store. Choose from: {list(STORES.keys())}")
            sys.exit(1)
        crawl_store(args.store, cls, date)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
