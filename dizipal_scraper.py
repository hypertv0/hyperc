import requests
from bs4 import BeautifulSoup
import re
import concurrent.futures
import time
import random
import json

# --- AYARLAR ---
MAX_WORKERS = 5
MAX_PAGES_PER_CATEGORY = 50 # Her kategori için maksimum kaç sayfa taranacak?
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://google.com",
    "X-Requested-With": "XMLHttpRequest" # Infinite Scroll taklidi için kritik
})

def get_current_domain():
    try:
        url = "https://raw.githubusercontent.com/Kraptor123/domainListesi/refs/heads/main/eklenti_domainleri.txt"
        response = session.get(url, timeout=10)
        for line in response.text.splitlines():
            if line.strip().startswith("DiziPal"):
                domain = line.split(":")[-1].strip()
                if not domain.startswith("http"):
                    domain = "https://" + domain
                print(f"[+] Güncel Domain: {domain}")
                return domain
    except:
        pass
    return "https://dizipal1217.com"

BASE_URL = get_current_domain()

def get_real_stream_url(page_url):
    """Linkin içindeki video kaynağını (m3u8) bulur. Dizi ise son bölüme gider."""
    try:
        # Rastgele bekleme (IP ban yememek için)
        time.sleep(random.uniform(0.1, 0.4))
        
        res = session.get(page_url, timeout=10)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. Direkt video var mı?
        iframe = soup.select_one('.series-player-container iframe') or soup.select_one('div#vast_new iframe')
        if iframe:
            src = iframe.get('src')
            if src:
                # Iframe kaynağını çözümle
                iframe_res = session.get(src, headers={"Referer": BASE_URL}, timeout=10)
                match = re.search(r'file:"([^"]+)"', iframe_res.text)
                if match:
                    return match.group(1)
        
        # 2. Video yoksa burası bir dizi sayfası olabilir, son bölüme git.
        episodes = soup.select('div.episode-item a') or soup.select('.episodes-list a') or soup.select('.episodes a')
        if episodes:
            first_ep_link = episodes[0].get('href')
            if not first_ep_link.startswith("http"):
                first_ep_link = BASE_URL + first_ep_link
            
            # Sonsuz döngü koruması: Eğer link aynı sayfaya çıkmıyorsa git
            if first_ep_link != page_url:
                return get_real_stream_url(first_ep_link)

    except Exception as e:
        pass
    return None

def process_item(item, category_name):
    """Bulunan içeriği işler ve m3u formatına çevirir."""
    try:
        # HTML Element ise
        if hasattr(item, 'select_one'):
            title_tag = item.select_one('.title') or item.select_one('h5') or item.select_one('.name')
            link_tag = item.select_one('a')
            img_tag = item.select_one('img')
            
            if not title_tag or not link_tag: return None
            
            title = title_tag.text.strip()
            link = link_tag.get('href')
            poster = img_tag.get('src') if img_tag else ""
        
        # JSON objesi ise (API'den gelirse)
        elif isinstance(item, dict):
            title = item.get('title', 'Bilinmeyen İçerik')
            link = item.get('url') or item.get('permalink')
            poster = item.get('poster') or item.get('thumbnail') or ""
        else:
            return None

        if not link: return None
        
        if not link.startswith("http"):
            link = BASE_URL + link

        stream_url = get_real_stream_url(link)
        
        if stream_url:
            m3u = f'#EXTINF:-1 group-title="{category_name}" tvg-logo="{poster}", {title}\n'
            m3u += f'#EXTVLCOPT:http-referrer={BASE_URL}/\n'
            m3u += f'#EXTHTTP:{{"Referer": "{BASE_URL}/"}}\n'
            m3u += f'{stream_url}\n'
            return m3u
    except:
        pass
    return None

def scrape_category(start_path, category_name):
    print(f"\n🚀 KATEGORİ: {category_name}")
    entries = []
    
    page_count = 1
    current_base_url = f"{BASE_URL}{start_path}"
    
    while page_count <= MAX_PAGES_PER_CATEGORY:
        # --- Sayfalama Mantığı (Infinite Scroll için ?page=X yapısı) ---
        if page_count == 1:
            target_url = current_base_url
        else:
            if "?" in current_base_url:
                target_url = f"{current_base_url}&page={page_count}"
            else:
                target_url = f"{current_base_url}?page={page_count}"
        
        print(f"   📄 Sayfa {page_count} taranıyor... [{target_url}]")
        
        try:
            res = session.get(target_url, timeout=15)
            
            # Eğer anasayfaya atarsa kategori bitmiştir
            if res.url == BASE_URL or res.status_code == 404:
                print("   🛑 Sayfa sonuna gelindi.")
                break
            
            # İçerik JSON mu HTML mi kontrol et
            items = []
            try:
                # Bazı sayfalar JSON dönebilir
                json_data = res.json()
                if 'html' in json_data:
                    # JSON içinde HTML dönüyorsa
                    soup = BeautifulSoup(json_data['html'], 'html.parser')
                    items = soup.select('div.episode-item') + soup.select('article.type2 ul li') + soup.select('.item')
                elif isinstance(json_data, list):
                    items = json_data
                elif 'items' in json_data:
                    items = json_data['items']
            except:
                # JSON değilse Standart HTML parse et
                soup = BeautifulSoup(res.text, 'html.parser')
                items = soup.select('div.episode-item') + soup.select('article.type2 ul li') + soup.select('.item')
            
            # Gereksiz elemanları temizle (Sadece linki olanlar)
            valid_items = []
            for i in items:
                if hasattr(i, 'select_one') and i.select_one('a'):
                    valid_items.append(i)
                elif isinstance(i, dict) and ('url' in i or 'permalink' in i):
                    valid_items.append(i)
            
            if not valid_items:
                print("   ⚠️ Bu sayfada içerik bulunamadı (Liste boş).")
                break
            
            # Paralel İşleme
            page_entries = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(process_item, item, category_name) for item in valid_items]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        page_entries.append(result)
            
            if page_entries:
                entries.extend(page_entries)
                print(f"   ✅ {len(page_entries)} içerik eklendi.")
            else:
                print("   ❌ İçerikler tarandı ama video linki çıkarılamadı.")

            page_count += 1
            
        except Exception as e:
            print(f"   🔥 Hata: {e}")
            break
            
    return entries

def main():
    # Kategori Listesi
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
    
    with open("dizipal.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        
    total = 0
    for path, name in categories:
        cat_data = scrape_category(path, name)
        if cat_data:
            with open("dizipal.m3u", "a", encoding="utf-8") as f:
                f.writelines(cat_data)
            total += len(cat_data)
            print(f"💾 {name} tamamlandı. (Kategori Toplamı: {len(cat_data)})")
        
        # Kategori geçişinde bekleme
        time.sleep(2)
            
    print(f"\n🎉 BİTTİ! Toplam {total} link 'dizipal.m3u' dosyasına yazıldı.")

if __name__ == "__main__":
    main()
