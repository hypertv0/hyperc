import requests
from bs4 import BeautifulSoup
import re
import concurrent.futures
import time
import random
from urllib.parse import urljoin

# --- AYARLAR ---
MAX_WORKERS = 5   # Dizi içlerine gireceği için worker sayısını düşük tutalım, ban yemeyelim.
MAX_PAGES_PER_CATEGORY = 50 # Her kategori için taranacak maksimum sayfa (Çok artırma, süre yetmez)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://google.com"
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
    return "https://dizipal1217.com" # Fallback

BASE_URL = get_current_domain()

def get_real_stream_url(page_url):
    """
    Verilen sayfa linkine gider ve video (m3u8) linkini bulur.
    Eğer bu bir dizi sayfasıysa, son bölümü bulup oraya gider.
    """
    try:
        time.sleep(random.uniform(0.1, 0.5)) # Nezaket beklemesi
        res = session.get(page_url, timeout=10)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. Senaryo: Sayfada direkt video var mı? (Film veya Bölüm Sayfası)
        iframe = soup.select_one('.series-player-container iframe') or soup.select_one('div#vast_new iframe')
        if iframe:
            src = iframe.get('src')
            if src:
                # Iframe kaynağını çözümle
                iframe_res = session.get(src, headers={"Referer": BASE_URL}, timeout=10)
                match = re.search(r'file:"([^"]+)"', iframe_res.text)
                if match:
                    return match.group(1)
        
        # 2. Senaryo: Video yok, burası bir Dizi Tanıtım Sayfası mı?
        # Bölümleri bulalım.
        episodes = soup.select('div.episode-item a') or soup.select('.episodes-list a')
        if episodes:
            # Genellikle en üstteki veya listedeki son bölüm en günceldir.
            # DiziPal yapısında genelde son eklenenler üstte olur ya da sezon listesi vardır.
            # İlk bulduğumuz bölüm linkine gidelim (Son Bölüm mantığı)
            first_ep_link = episodes[0].get('href')
            if not first_ep_link.startswith("http"):
                first_ep_link = BASE_URL + first_ep_link
            
            # Recursive (Kendini tekrar çağırma): Bölüm sayfasına git ve oradan video çek
            # Sonsuz döngüye girmemesi için URL kontrolü yapılabilir ama basit tutuyoruz.
            if first_ep_link != page_url:
                return get_real_stream_url(first_ep_link)

    except Exception as e:
        pass
    return None

def process_item(item, category_name):
    try:
        title_tag = item.select_one('.title') or item.select_one('h5') or item.select_one('.name')
        link_tag = item.select_one('a')
        img_tag = item.select_one('img')
        
        if not title_tag or not link_tag: return None
        
        title = title_tag.text.strip()
        link = link_tag.get('href')
        poster = img_tag.get('src') if img_tag else ""
        
        if not link.startswith("http"):
            link = BASE_URL + link

        # İçeriğin gerçek yayın linkini bul (Gerekirse dizi içine girer)
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

def get_next_page_link(soup):
    """HTML içinden 'Sonraki Sayfa' linkini bulur."""
    # Sitenin yapısına göre 'Next', 'ileri', 'fa-angle-right' veya pagination class'larını arar.
    next_a = soup.select_one('a.next') or soup.select_one('a.page-link[rel="next"]') or soup.select_one('li.next a')
    
    if next_a:
        href = next_a.get('href')
        if href and href != "#":
            if not href.startswith("http"):
                return BASE_URL + href
            return href
    return None

def scrape_category(start_path, category_name):
    print(f"\n🚀 KATEGORİ: {category_name}")
    entries = []
    
    current_url = f"{BASE_URL}{start_path}"
    page_count = 1
    
    while current_url and page_count <= MAX_PAGES_PER_CATEGORY:
        print(f"   📄 Sayfa {page_count} taranıyor... [{current_url}]")
        
        try:
            res = session.get(current_url, timeout=15)
            if res.status_code != 200:
                print("   🛑 Erişim hatası.")
                break
                
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # İçerik kartlarını bul
            items = soup.select('div.episode-item') + soup.select('article.type2 ul li') + soup.select('.item')
            # Gereksiz/yanlış elementleri temizle (Varsa)
            items = [i for i in items if i.select_one('a')]
            
            if not items:
                print("   ⚠️ Bu sayfada içerik bulunamadı.")
                break
                
            # Linkleri işle
            page_entries = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(process_item, item, category_name) for item in items]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        page_entries.append(result)
            
            if page_entries:
                entries.extend(page_entries)
                print(f"   ✅ {len(page_entries)} içerik eklendi.")
            else:
                print("   ❌ İçerik var ama video linki çıkarılamadı.")

            # Sonraki sayfayı bul
            next_url = get_next_page_link(soup)
            
            # Eğer next linki yoksa, manuel url tahmin etmeyi dene (Fallback)
            if not next_url:
                # /page/1 -> /page/2 mantığı
                if "/page/" in current_url:
                    current_page_num = int(re.search(r'/page/(\d+)', current_url).group(1))
                    next_url = re.sub(r'/page/\d+', f'/page/{current_page_num + 1}', current_url)
                elif page_count == 1:
                    # İlk sayfa, sonuna /page/2 ekle
                    next_url = f"{current_url}/page/2" if current_url.endswith('/') else f"{current_url}/page/2"
                
                # Oluşturulan linkin geçerli olup olmadığını bir sonraki döngüde anlayacağız
            
            current_url = next_url
            page_count += 1
            
        except Exception as e:
            print(f"   🔥 Hata: {e}")
            break
            
    return entries

def main():
    categories = [
        ("/diziler/son-bolumler", "Son Bölümler"),
        ("/filmler", "Filmler"),
        ("/diziler", "Diziler"), # Artık dizi içlerine girecek
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
            print(f"💾 {name} tamamlandı. Toplam: {total}")
            
    print(f"\n🎉 BİTTİ! Toplam {total} link.")

if __name__ == "__main__":
    main()
