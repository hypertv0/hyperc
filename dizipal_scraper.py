import requests
from bs4 import BeautifulSoup
import re
import concurrent.futures
import time
import random

# --- AYARLAR ---
MAX_WORKERS = 10  # Hız için iş parçacığı sayısı (Çok artırırsan IP ban yersin)
MAX_PAGES = 500   # Her kategori için taranacak maksimum sayfa sayısı (Sonsuz döngüyü engellemek için güvenlik sınırı)
RETRY_COUNT = 3   # Başarısız istekleri kaç kez denesin

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://google.com"
}

def get_current_domain():
    try:
        url = "https://raw.githubusercontent.com/Kraptor123/domainListesi/refs/heads/main/eklenti_domainleri.txt"
        response = requests.get(url, timeout=10)
        content = response.text
        for line in content.splitlines():
            if line.strip().startswith("DiziPal"):
                domain = line.split(":")[-1].strip()
                if not domain.startswith("http"):
                    domain = "https://" + domain
                print(f"[+] Güncel Domain: {domain}")
                return domain
    except Exception as e:
        print(f"[-] Domain bulunamadı, varsayılan kullanılıyor: {e}")
    return "https://dizipal1217.com"

BASE_URL = get_current_domain()
HEADERS["Referer"] = BASE_URL + "/"

def get_iframe_source(url):
    """Linkin içindeki m3u8 dosyasını bulur."""
    for _ in range(RETRY_COUNT):
        try:
            res = requests.get(url, headers=HEADERS, timeout=10)
            if res.status_code != 200: return None
            
            soup = BeautifulSoup(res.text, 'html.parser')
            iframe = soup.select_one('.series-player-container iframe') or soup.select_one('div#vast_new iframe')
            
            if iframe:
                src = iframe.get('src')
                if src:
                    # Iframe'e git
                    iframe_res = requests.get(src, headers={"Referer": BASE_URL}, timeout=10)
                    match = re.search(r'file:"([^"]+)"', iframe_res.text)
                    if match:
                        return match.group(1)
            break # Başarılıysa döngüden çık
        except:
            time.sleep(1) # Hata olursa 1sn bekle tekrar dene
    return None

def process_item(item, category_name):
    """Tek bir içeriği işler."""
    try:
        # Site yapısına göre başlık ve link seçicileri
        title_tag = item.select_one('.title') or item.select_one('h5') or item.select_one('.name')
        link_tag = item.select_one('a')
        img_tag = item.select_one('img')
        
        if not title_tag or not link_tag:
            return None
            
        title = title_tag.text.strip()
        link = link_tag.get('href')
        poster = img_tag.get('src') if img_tag else ""
        
        if not link.startswith("http"):
            link = BASE_URL + link

        stream_url = get_iframe_source(link)
        
        if stream_url:
            # M3U Entry
            m3u = f'#EXTINF:-1 group-title="{category_name}" tvg-logo="{poster}", {title}\n'
            m3u += f'#EXTVLCOPT:http-referrer={BASE_URL}/\n'
            m3u += f'#EXTHTTP:{{"Referer": "{BASE_URL}/"}}\n'
            m3u += f'{stream_url}\n'
            return m3u
    except:
        return None
    return None

def scrape_category_pages(base_path, category_name):
    """Bir kategorinin TÜM sayfalarını tarar."""
    print(f"\n🚀 KATEGORİ BAŞLIYOR: {category_name}")
    category_m3u_entries = []
    
    page = 1
    empty_streak = 0 # Boş sayfa sayacı

    while page <= MAX_PAGES:
        # Sayfa URL yapısını oluştur
        if page == 1:
            target_url = f"{BASE_URL}{base_path}"
        else:
            # Genellikle yapı /page/2 şeklindedir
            target_url = f"{BASE_URL}{base_path}/page/{page}"
        
        try:
            res = requests.get(target_url, headers=HEADERS, timeout=15)
            
            # Eğer 404 dönerse veya anasayfaya yönlendirirse kategori bitmiştir
            if res.status_code == 404 or res.url == BASE_URL:
                print(f"   🛑 Sayfa {page} bulunamadı. Kategori bitti.")
                break

            soup = BeautifulSoup(res.text, 'html.parser')
            
            # İçerikleri bul
            items = soup.select('div.episode-item') + soup.select('article.type2 ul li')
            
            if not items:
                print(f"   ⚠️ Sayfa {page} boş. (İçerik bulunamadı)")
                break
                
            print(f"   📄 Sayfa {page} taranıyor... ({len(items)} içerik)")
            
            # Paralel işlem ile linkleri çek
            current_page_entries = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(process_item, item, category_name) for item in items]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        current_page_entries.append(result)
            
            if len(current_page_entries) == 0:
                print("   ❌ Bu sayfadan oynatılabilir link çıkmadı.")
                # Link çıkmasa bile sayfada içerik vardı, o yüzden devam et
            else:
                category_m3u_entries.extend(current_page_entries)
                print(f"   ✅ Sayfa {page} tamamlandı. {len(current_page_entries)} link eklendi.")

            page += 1
            # IP Ban yememek için sayfa geçişlerinde rastgele bekleme
            time.sleep(random.uniform(0.5, 1.5))
            
        except Exception as e:
            print(f"   🔥 Hata (Sayfa {page}): {e}")
            break

    return category_m3u_entries

def main():
    # Kategori Listesi
    categories = [
        ("/diziler/son-bolumler", "Son Bölümler"),
        ("/filmler", "Filmler"), # "Yeni Filmler" yerine genel "Filmler" daha çok içerik verir
        ("/diziler", "Diziler"),
        ("/koleksiyon/netflix", "Netflix"),
        ("/koleksiyon/exxen", "Exxen"),
        ("/koleksiyon/blutv", "BluTV"),
        ("/koleksiyon/disney", "Disney+"),
        ("/koleksiyon/amazon-prime", "Amazon Prime"),
        ("/koleksiyon/gain", "Gain"),
        ("/tur/mubi", "Mubi")
    ]
    
    # Dosyayı sıfırla ve başlığı yaz
    with open("dizipal.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")

    total_added = 0
    
    for path, name in categories:
        entries = scrape_category_pages(path, name)
        
        # Her kategori bittiğinde dosyaya ekle (RAM şişmesin diye)
        if entries:
            with open("dizipal.m3u", "a", encoding="utf-8") as f:
                f.writelines(entries)
            total_added += len(entries)
            print(f"💾 {name} kaydedildi. (Toplam: {total_added})")
        
    print(f"\n🎉 TÜM İŞLEM BİTTİ! Toplam {total_added} içerik 'dizipal.m3u' dosyasına kaydedildi.")

if __name__ == "__main__":
    main()
