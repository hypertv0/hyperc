import cloudscraper
from bs4 import BeautifulSoup
import re
import time
import random
import concurrent.futures
import sys
import json

# --- AYARLAR ---
MAX_WORKERS = 10      # Aynı anda taranacak video sayısı
MAX_SCROLLS = 50      # "Daha Fazla Göster"e kaç kere basılsın? (Her basışta ~20 film gelir)
RETRY_COUNT = 3       # Hata durumunda deneme sayısı

def log(message):
    """Anlık çıktı vermek için flush=True kullanır."""
    print(message, flush=True)

# CloudScraper Ayarları
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

# Headerlar: Siteye "Ben bir tarayıcıyım ve AJAX isteği atıyorum" diyoruz.
scraper.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://google.com",
    "X-Requested-With": "XMLHttpRequest", # BU ÇOK ÖNEMLİ (AJAX İsteği Olduğunu Belirtir)
    "Origin": "https://dizipal1217.com",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
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
API_URL = f"{BASE_URL}/api/load-movies" # API Adresi

# Headerlardaki Origin ve Referer'ı güncel domain ile güncelle
scraper.headers.update({
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL
})

def get_video_source(url):
    """Linkin içindeki video kaynağını bulur, gerekirse dizi içine girer."""
    for _ in range(RETRY_COUNT):
        try:
            time.sleep(random.uniform(0.1, 0.3))
            res = scraper.get(url)
            if res.status_code != 200: return None
            
            soup = BeautifulSoup(res.text, 'html.parser')

            # 1. Direkt Video Var mı? (Film veya Bölüm)
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
            # En son eklenen bölüm genelde en üsttedir veya listededir.
            episodes = soup.select('div.episode-item a') or \
                       soup.select('.episodes-list a') or \
                       soup.select('ul.episodes li a')
            
            if episodes:
                # İlk bölümü al
                first_ep = episodes[0].get('href')
                if not first_ep.startswith("http"):
                    first_ep = BASE_URL + first_ep
                
                if first_ep != url:
                    return get_video_source(first_ep)

            return None

        except:
            continue
    return None

def process_single_content(item, category_name):
    """Tek bir içerik kartını işler."""
    try:
        # HTML elementinden verileri çek
        link_tag = item.select_one('a')
        if not link_tag: return None

        title_tag = item.select_one('.title') or item.select_one('h5')
        title = title_tag.text.strip() if title_tag else link_tag.get('title', 'Bilinmeyen')

        img_tag = item.select_one('img')
        poster = img_tag.get('src') if img_tag else ""
        if poster and not poster.startswith("http"):
            poster = BASE_URL + poster # Relative path düzeltme

        link = link_tag.get('href')
        if not link.startswith("http"):
            link = BASE_URL + link

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

def scrape_category(path, category_name):
    log(f"\n🚀 KATEGORİ BAŞLIYOR: {category_name}")
    entries = []
    
    # 1. İlk Sayfayı Çek (GET İsteği)
    first_url = f"{BASE_URL}{path}"
    log(f"   📄 Başlangıç Sayfası: {first_url}")
    
    try:
        res = scraper.get(first_url)
        soup = BeautifulSoup(res.text, 'html.parser')
    except Exception as e:
        log(f"   🔥 Erişim Hatası: {e}")
        return []

    scroll_count = 0
    
    while scroll_count <= MAX_SCROLLS:
        # İçerikleri Bul (Verdiğin HTML'deki yapıya göre)
        # article.type2 ul li -> Filmler
        # div.episode-item -> Son Bölümler
        items = soup.select('article.movie-type-genres ul li') + \
                soup.select('div.episode-item') + \
                soup.select('article.type2 li')
        
        # Filtreleme
        items = [i for i in items if i.select_one('a')]
        
        if not items:
            log("   ⚠️ İçerik bulunamadı veya liste bitti.")
            break

        # İşlenecek öğeleri belirle (Öncekileri tekrar işlememek için mantık kurulabilir ama
        # şu an API yeni veriyi html olarak append ettiği için, sadece yeni gelenleri işlememiz lazım.
        # Basitlik adına: API response sadece yeni veri döner, onu parse ederiz.)
        
        # Paralel İşleme
        found_on_load = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_single_content, item, category_name) for item in items]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    entries.append(result)
                    found_on_load += 1
                    print(".", end="", flush=True) # İlerleme çubuğu
        
        print("") # Satır sonu
        if found_on_load > 0:
            log(f"   ✅ Bu yüklemede {found_on_load} içerik eklendi.")

        # --- SONRAKİ SAYFA (AJAX LOAD MORE) ---
        
        # Listedeki SON elemanın 'data-id' özelliğini bul
        # HTML örneğinde: <a ... data-id="Wz8YoLmPu9Ia65kYxL3F3dJPLWhMdLK0CBqZEC8GoJ0=">
        last_item = items[-1].select_one('a')
        if not last_item or not last_item.has_attr('data-id'):
            log("   🏁 Daha fazla yükle butonu/verisi bulunamadı. Kategori bitti.")
            break
            
        last_data_id = last_item.get('data-id')
        
        # API'ye POST isteği at
        # Parametreler sitedeki JS kodundan alındı: movie, year, tur, siralama
        payload = {
            'movie': last_data_id,
            'year': '',
            'tur': '',
            'siralama': ''
        }
        
        try:
            time.sleep(1) # API'yi boğmamak için bekle
            api_res = scraper.post(API_URL, data=payload)
            
            try:
                json_data = api_res.json()
            except:
                log("   ❌ API geçerli JSON döndürmedi. Bitti.")
                break
                
            # Eğer 'end': true ise bitmiştir
            if json_data.get('end') == True:
                log("   🏁 İçerik sonuna gelindi (API End).")
                break
                
            # Yeni HTML içeriği geldi mi?
            new_html = json_data.get('html')
            if not new_html:
                log("   ⚠️ API boş içerik döndü.")
                break
                
            # Yeni HTML'i Soup'a çevirip döngüye devam et
            soup = BeautifulSoup(new_html, 'html.parser')
            scroll_count += 1
            log(f"   🔄 Daha fazla yüklendi ({scroll_count}. kaydırma)...")
            
        except Exception as e:
            log(f"   🔥 API Hatası: {e}")
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
        ("/koleksiyon/gain", "Gain"),
        ("/tur/mubi", "Mubi")
    ]
    
    # Dosyayı sıfırla
    with open("dizipal.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        
    total_count = 0
    
    for path, name in categories:
        cat_data = scrape_category(path, name)
        if cat_data:
            # Tekrarlananları temizle (Set kullanarak)
            unique_data = list(set(cat_data))
            
            with open("dizipal.m3u", "a", encoding="utf-8") as f:
                f.writelines(unique_data)
            
            count = len(unique_data)
            total_count += count
            log(f"💾 {name} KAYDEDİLDİ. (+{count} içerik)")
        
        time.sleep(2)

    log(f"\n🎉 TÜM İŞLEM BİTTİ! Toplam {total_count} içerik 'dizipal.m3u' dosyasına yazıldı.")

if __name__ == "__main__":
    main()
