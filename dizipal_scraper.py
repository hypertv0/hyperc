import cloudscraper
from bs4 import BeautifulSoup
import re
import time
import random
import concurrent.futures

# --- AYARLAR ---
MAX_WORKERS = 5   # Aynı anda kaç link kontrol edilsin? (Çok artırma ban yersin)
MAX_PAGES = 30    # Her kategori için kaç sayfa taransın? (İdeal: 20-50 arası)
RETRY_COUNT = 3   # Başarısız olursa kaç kez denesin?

# CloudScraper: Cloudflare korumasını aşmak için gerekli
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

def get_current_domain():
    """GitHub'dan güncel domaini çeker."""
    try:
        url = "https://raw.githubusercontent.com/Kraptor123/domainListesi/refs/heads/main/eklenti_domainleri.txt"
        response = scraper.get(url).text
        for line in response.splitlines():
            if line.strip().startswith("DiziPal"):
                domain = line.split(":")[-1].strip()
                if not domain.startswith("http"):
                    domain = "https://" + domain
                print(f"[+] Güncel Domain: {domain}")
                return domain
    except Exception as e:
        print(f"[-] Domain çekilemedi: {e}")
    return "https://dizipal1217.com" # Yedek

BASE_URL = get_current_domain()

def get_video_source(url):
    """
    Verilen URL'deki m3u8 linkini bulur.
    Eğer dizi sayfasıysa, son bölüme gider.
    """
    for _ in range(RETRY_COUNT):
        try:
            time.sleep(random.uniform(0.1, 0.5))
            res = scraper.get(url)
            
            if res.status_code != 200: return None
            soup = BeautifulSoup(res.text, 'html.parser')

            # --- SENARYO 1: Video Sayfası ---
            # Smali kodundaki mantık: .series-player-container iframe veya div#vast_new iframe
            iframe = soup.select_one('.series-player-container iframe') or \
                     soup.select_one('div#vast_new iframe') or \
                     soup.select_one('iframe[src*="player"]')

            if iframe:
                src = iframe.get('src')
                if src:
                    if src.startswith("//"): src = "https:" + src
                    # Iframe içine gir
                    iframe_res = scraper.get(src, headers={"Referer": BASE_URL})
                    # Regex ile file:"..." veya dosya:"..." ara
                    match = re.search(r'(?:file|source)\s*:\s*"([^"]+)"', iframe_res.text)
                    if match:
                        return match.group(1)

            # --- SENARYO 2: Dizi Ana Sayfası (Bölüm Listesi) ---
            # Eğer video yoksa, burası bir dizi sayfasıdır. Son bölümü bulup içine girelim.
            episodes = soup.select('div.episode-item a') or \
                       soup.select('.episodes-list a') or \
                       soup.select('ul.episodes li a')
            
            if episodes:
                # Genellikle listenin başındaki veya sonundaki en günceldir.
                # Biz ilkini (üsttekini) alalım.
                first_ep = episodes[0].get('href')
                if not first_ep.startswith("http"):
                    first_ep = BASE_URL + first_ep
                
                # Sonsuz döngüye girmesin diye link farklı mı kontrol et
                if first_ep != url:
                    return get_video_source(first_ep) # Recursive çağrı

            return None # Bulunamadı

        except Exception as e:
            time.sleep(1)
            continue
    return None

def process_single_content(item, category_name):
    """Tek bir içerik kartını işler."""
    try:
        # Kotlin kodundaki ve olası yedek seçiciler
        # Başlık
        title_tag = item.select_one('.title') or \
                    item.select_one('h5') or \
                    item.select_one('.name') or \
                    item.select_one('h2') or \
                    item.select_one('a[title]')
        
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

        # Videoyu bul
        stream_url = get_video_source(link)
        
        if stream_url:
            m3u = f'#EXTINF:-1 group-title="{category_name}" tvg-logo="{poster}", {title}\n'
            m3u += f'#EXTVLCOPT:http-referrer={BASE_URL}/\n'
            m3u += f'#EXTHTTP:{{"Referer": "{BASE_URL}/"}}\n'
            m3u += f'{stream_url}\n'
            return m3u

    except Exception:
        return None
    return None

def scrape_category(path, category_name):
    print(f"\n🚀 KATEGORİ: {category_name}")
    entries = []
    
    page = 1
    while page <= MAX_PAGES:
        # URL Oluşturma: DiziPal genelde /page/2/ yapısını kullanır.
        if page == 1:
            target_url = f"{BASE_URL}{path}"
        else:
            # Standart WordPress/DiziPal sayfalama yapısı
            target_url = f"{BASE_URL}{path}/page/{page}/"

        print(f"   📄 Sayfa {page} taranıyor... [{target_url}]")
        
        try:
            res = scraper.get(target_url)
            
            # Sayfa yoksa (404) veya anasayfaya yönlendiriyorsa dur.
            if res.status_code == 404 or res.url.rstrip('/') == BASE_URL.rstrip('/'):
                print("   🛑 Sayfa sonuna gelindi.")
                break
                
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # Seçiciler (Kotlin kodundan + Genişletilmiş)
            # div.episode-item -> Kotlin'de 'son-bolumler' için
            # article.type2 ul li -> Kotlin'de diğerleri için
            # .item, .movie-item -> Yedekler
            items = soup.select('div.episode-item') + \
                    soup.select('article.type2 ul li') + \
                    soup.select('article.type2 li') + \
                    soup.select('.item') + \
                    soup.select('.movie-item')
            
            # HTML içinden 'a' etiketi olmayanları temizle
            items = [i for i in items if i.select_one('a')]
            
            if not items:
                print("   ⚠️ İçerik bulunamadı (Cloudflare engeli olabilir veya liste bitti).")
                # Eğer ilk sayfada bile bulamadıysa, bir sorun var demektir ama döngüyü kıralım.
                break

            # Paralel İşleme
            current_page_entries = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(process_single_content, item, category_name) for item in items]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        current_page_entries.append(result)
            
            if current_page_entries:
                entries.extend(current_page_entries)
                print(f"   ✅ {len(current_page_entries)} link eklendi.")
            else:
                print("   ❌ Bu sayfadan video linki çıkarılamadı.")

            page += 1
            
        except Exception as e:
            print(f"   🔥 Hata: {e}")
            break
            
    return entries

def main():
    # Eklenti Kodundaki Kategoriler (DiziPal.kt Satır 46-63)
    categories = [
        ("/diziler/son-bolumler", "Son Bölümler"),
        ("/filmler", "Filmler"),
        ("/diziler", "Diziler"),
        ("/koleksiyon/netflix", "Netflix"),
        ("/koleksiyon/exxen", "Exxen"),
        ("/koleksiyon/blutv", "BluTV"),
        ("/koleksiyon/disney", "Disney+"),
        ("/koleksiyon/amazon-prime", "Amazon Prime"),
        ("/koleksiyon/tod-bein", "TOD (beIN)"), # Bunu da ekledim kodda vardı
        ("/koleksiyon/gain", "Gain"),
        ("/tur/mubi", "Mubi")
    ]
    
    # M3U Dosyasını Başlat
    with open("dizipal.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
    
    total_links = 0
    
    for path, name in categories:
        cat_data = scrape_category(path, name)
        if cat_data:
            with open("dizipal.m3u", "a", encoding="utf-8") as f:
                f.writelines(cat_data)
            total_links += len(cat_data)
            print(f"💾 {name} kaydedildi. (Şu ana kadar toplam: {total_links})")
        
        time.sleep(2) # Kategori arası bekleme

    print(f"\n🎉 İŞLEM TAMAMLANDI! Toplam {total_links} içerik 'dizipal.m3u' dosyasına yazıldı.")

if __name__ == "__main__":
    main()
