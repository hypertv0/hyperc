import cloudscraper
from bs4 import BeautifulSoup
import re
import time
import random
import concurrent.futures
import sys
import urllib.parse

# --- AYARLAR ---
MAX_WORKERS = 5      # Aynı anda taranacak video sayısı
MAX_PAGES = 50       # Her kategori için güvenlik sınırı
RETRY_COUNT = 3      # Hata durumunda deneme sayısı

# Çıktıların anlık görünmesi için print fonksiyonunu özelleştiriyoruz
def log(message):
    print(message, flush=True)

# CloudScraper Ayarları (Anti-Bot Koruması İçin)
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

# Header ayarları (Infinite Scroll taklidi)
scraper.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://google.com",
    "X-Requested-With": "XMLHttpRequest" # Bu satır sitenin bizi AJAX isteği sanmasını sağlar
})

def get_current_domain():
    try:
        url = "https://raw.githubusercontent.com/Kraptor123/domainListesi/refs/heads/main/eklenti_domainleri.txt"
        response = scraper.get(url).text
        for line in response.splitlines():
            if line.strip().startswith("DiziPal"):
                domain = line.split(":")[-1].strip()
                if not domain.startswith("http"):
                    domain = "https://" + domain
                log(f"[+] Güncel Domain Bulundu: {domain}")
                return domain
    except:
        pass
    return "https://dizipal1217.com"

BASE_URL = get_current_domain()

def get_video_source(url):
    """Linkin içindeki video kaynağını bulur, gerekirse dizi içine girer."""
    for _ in range(RETRY_COUNT):
        try:
            time.sleep(random.uniform(0.1, 0.5))
            res = scraper.get(url)
            if res.status_code != 200: return None
            
            soup = BeautifulSoup(res.text, 'html.parser')

            # 1. Direkt Video Var mı?
            iframe = soup.select_one('.series-player-container iframe') or \
                     soup.select_one('div#vast_new iframe') or \
                     soup.select_one('iframe[src*="player"]')

            if iframe:
                src = iframe.get('src')
                if src:
                    if src.startswith("//"): src = "https:" + src
                    # Iframe içine girip m3u8 ara
                    iframe_res = scraper.get(src, headers={"Referer": BASE_URL})
                    match = re.search(r'(?:file|source)\s*:\s*"([^"]+)"', iframe_res.text)
                    if match:
                        return match.group(1)

            # 2. Dizi Sayfası mı? (Bölüm Listesi)
            episodes = soup.select('div.episode-item a') or \
                       soup.select('.episodes-list a') or \
                       soup.select('ul.episodes li a')
            
            if episodes:
                # İlk bölümü (genelde son eklenen) al
                first_ep = episodes[0].get('href')
                if not first_ep.startswith("http"):
                    first_ep = BASE_URL + first_ep
                
                if first_ep != url:
                    return get_video_source(first_ep)

            return None # Bulunamadı

        except:
            time.sleep(1)
            continue
    return None

def process_single_content(item, category_name):
    """Tek bir içerik kartını işler ve log basar."""
    try:
        # Başlık
        title_tag = item.select_one('.title') or item.select_one('h5') or \
                    item.select_one('.name') or item.select_one('a[title]')
        
        # Link
        link_tag = item.select_one('a')
        
        # Resim
        img_tag = item.select_one('img')

        if not title_tag or not link_tag:
            return None

        title = title_tag.text.strip()
        if not title and link_tag.has_attr('title'):
            title = link_tag['title']
            
        link = link_tag.get('href')
        poster = img_tag.get('src') if img_tag else ""
        
        if poster and not poster.startswith("http"):
            poster = BASE_URL + poster
        
        if not link.startswith("http"):
            link = BASE_URL + link

        # Loglama (Hangi içeriğe baktığını gör)
        # log(f"      Kontrol ediliyor: {title}...") 

        stream_url = get_video_source(link)
        
        if stream_url:
            m3u = f'#EXTINF:-1 group-title="{category_name}" tvg-logo="{poster}", {title}\n'
            m3u += f'#EXTVLCOPT:http-referrer={BASE_URL}/\n'
            m3u += f'#EXTHTTP:{{"Referer": "{BASE_URL}/"}}\n'
            m3u += f'{stream_url}\n'
            return m3u
            
    except Exception:
        pass
    return None

def find_next_page(soup, current_url, current_page):
    """
    HTML içinden bir sonraki sayfa linkini bulmaya çalışır.
    Bulamazsa '?page=X' mantığını dener.
    """
    # 1. HTML içindeki 'Next' butonuna bak
    next_a = soup.select_one('a.next') or \
             soup.select_one('a.page-link[rel="next"]') or \
             soup.select_one('li.next a') or \
             soup.select_one('a.next-page')

    if next_a:
        href = next_a.get('href')
        if href and href != "#":
            if not href.startswith("http"):
                return BASE_URL + href
            return href

    # 2. Buton yoksa URL yapısını değiştirerek dene (Fallback)
    # Eğer URL'de /page/2/ yapısı yoksa, ?page=2 yapısını dene.
    if "/page/" not in current_url:
        parsed = urllib.parse.urlparse(current_url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        query['page'] = current_page + 1
        new_query = urllib.parse.urlencode(query)
        new_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
        return new_url
    
    # URL zaten /page/X/ formatındaysa ve buton bulunamadıysa muhtemelen sayfa bitmiştir.
    return None

def scrape_category(start_path, category_name):
    log(f"\n🚀 KATEGORİ BAŞLIYOR: {category_name}")
    entries = []
    
    page = 1
    current_url = f"{BASE_URL}{start_path}"
    
    while page <= MAX_PAGES:
        log(f"   📄 Sayfa {page} taranıyor... [{current_url}]")
        
        try:
            res = scraper.get(current_url, timeout=15)
            
            if res.status_code == 404:
                log("   🛑 Sayfa bulunamadı (404). Kategori bitti.")
                break
                
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # İçerik seçicileri
            items = soup.select('div.episode-item') + \
                    soup.select('article.type2 ul li') + \
                    soup.select('article.type2 li') + \
                    soup.select('.item') + \
                    soup.select('.movie-item')
            
            # Filtreleme (Link içerenler)
            items = [i for i in items if i.select_one('a')]
            
            if not items:
                log("   ⚠️ İçerik listesi boş. Kategori bitti.")
                break

            # Paralel Tarama
            found_on_page = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(process_single_content, item, category_name) for item in items]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        entries.append(result)
                        found_on_page += 1
                        # Anlık ilerleme göstermek için nokta koy
                        print(".", end="", flush=True)
            
            print("") # Satır sonu
            
            if found_on_page > 0:
                log(f"   ✅ Bu sayfadan {found_on_page} link eklendi.")
            else:
                log("   ❌ Bu sayfadan link çıkarılamadı.")

            # Sonraki sayfayı bul
            next_url = find_next_page(soup, current_url, page)
            
            if not next_url or next_url == current_url:
                log("   🏁 Sonraki sayfa bulunamadı. Kategori tamamlandı.")
                break
                
            current_url = next_url
            page += 1
            
        except Exception as e:
            log(f"   🔥 Kritik Hata: {e}")
            break
            
    return entries

def main():
    # Kategoriler
    categories = [
        ("/diziler/son-bolumler", "Son Bölümler"),
        ("/filmler", "Filmler"),
        ("/diziler", "Diziler"),
        ("/koleksiyon/netflix", "Netflix"),
        ("/koleksiyon/exxen", "Exxen"),
        ("/koleksiyon/blutv", "BluTV"),
        ("/koleksiyon/disney", "Disney+"),
        ("/koleksiyon/amazon-prime", "Amazon Prime"),
        ("/koleksiyon/gain",
