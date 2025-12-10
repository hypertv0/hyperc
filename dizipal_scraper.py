import requests
from bs4 import BeautifulSoup
import re
import concurrent.futures
import time

# --- AYARLAR ---
# GitHub Actions'da zaman aşımına uğramaması için maksimum iş parçacığı sayısı
MAX_WORKERS = 5
# User-Agent, tarayıcı gibi görünmek için şart
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
}

def get_current_domain():
    """
    Uygulamanın yaptığı gibi GitHub'dan güncel DiziPal domainini çeker.
    """
    try:
        url = "https://raw.githubusercontent.com/Kraptor123/domainListesi/refs/heads/main/eklenti_domainleri.txt"
        response = requests.get(url)
        content = response.text
        
        # Dosyayı satır satır oku ve DiziPal ile başlayanı bul
        for line in content.splitlines():
            if line.strip().startswith("DiziPal"):
                # Örnek satır: DiziPal : https://dizipal1217.com
                domain = line.split(":")[-1].strip()
                if not domain.startswith("http"):
                    domain = "https://" + domain
                print(f"[+] Güncel Domain Bulundu: {domain}")
                return domain
    except Exception as e:
        print(f"[-] Domain bulma hatası: {e}")
    
    # Fallback (Yedek)
    return "https://dizipal1217.com"

BASE_URL = get_current_domain()
HEADERS["Referer"] = BASE_URL + "/"

def get_iframe_source(url):
    """
    Video sayfasındaki iframe içerisinden m3u8 linkini ayıklar.
    """
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Smali kodundaki: .series-player-container iframe veya div#vast_new iframe
        iframe = soup.select_one('.series-player-container iframe')
        if not iframe:
            iframe = soup.select_one('div#vast_new iframe')
            
        if iframe:
            src = iframe.get('src')
            if src:
                # Iframe kaynağına git
                iframe_res = requests.get(src, headers={"Referer": BASE_URL}, timeout=10)
                # Regex ile file:"..." kısmını bul
                match = re.search(r'file:"([^"]+)"', iframe_res.text)
                if match:
                    return match.group(1)
    except Exception as e:
        # Hata olursa loglayabiliriz ama m3u dosyasını kirletmemek için pass geçiyoruz
        pass
    return None

def process_item(item, category_name):
    """
    Tek bir içeriği işler ve M3U formatında string döndürür.
    """
    try:
        title_tag = item.select_one('.title') or item.select_one('h5')
        link_tag = item.select_one('a')
        img_tag = item.select_one('img')
        
        if not title_tag or not link_tag:
            return None
            
        title = title_tag.text.strip()
        link = link_tag.get('href')
        poster = img_tag.get('src') if img_tag else ""
        
        if not link.startswith("http"):
            link = BASE_URL + link

        # Video linkini çek
        stream_url = get_iframe_source(link)
        
        if stream_url:
            # M3U Formatı
            # Cloudstream header eklemişti, biz de VLC/TiviMate uyumlu header ekleyelim
            m3u_entry = f'#EXTINF:-1 group-title="{category_name}" tvg-logo="{poster}", {title}\n'
            m3u_entry += f'#EXTVLCOPT:http-referrer={BASE_URL}/\n'
            m3u_entry += f'#EXTHTTP:{{"Referer": "{BASE_URL}/"}}\n'
            m3u_entry += f'{stream_url}\n'
            return m3u_entry
            
    except Exception as e:
        return None
    return None

def scrape_category(path, category_name):
    """
    Belirli bir kategori sayfasındaki içerikleri tarar.
    """
    full_url = f"{BASE_URL}{path}"
    print(f"[*] Taranıyor: {category_name} - {full_url}")
    
    entries = []
    try:
        res = requests.get(full_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # İçerik seçicileri (Smali kodundaki analizden)
        # Genellikle 'div.episode-item' (diziler için) veya 'article.type2 ul li' (filmler için)
        items = soup.select('div.episode-item') + soup.select('article.type2 ul li')
        
        print(f"    -> {len(items)} içerik bulundu. Linkler ayıklanıyor...")
        
        # Hız için paralel işlem
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_item, item, category_name) for item in items]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    entries.append(result)
                    
    except Exception as e:
        print(f"[-] Kategori hatası ({category_name}): {e}")
        
    return entries

def main():
    # Eklentideki Kategoriler (DiziPal.kt dosyasından alındı)
    categories = [
        ("/diziler/son-bolumler", "Son Bölümler"),
        ("/filmler", "Yeni Filmler"),
        ("/diziler", "Yeni Diziler"),
        ("/koleksiyon/netflix", "Netflix"),
        ("/koleksiyon/exxen", "Exxen"),
        ("/koleksiyon/blutv", "BluTV"),
        ("/koleksiyon/disney", "Disney+"),
        ("/koleksiyon/amazon-prime", "Amazon Prime"),
        ("/koleksiyon/gain", "Gain"),
        ("/tur/mubi", "Mubi")
    ]
    
    all_content = ["#EXTM3U\n"]
    
    for path, name in categories:
        category_entries = scrape_category(path, name)
        all_content.extend(category_entries)
        time.sleep(1) # Siteyi yormamak için kısa bekleme
        
    # Dosyayı kaydet
    with open("dizipal.m3u", "w", encoding="utf-8") as f:
        f.writelines(all_content)
        
    print(f"\n[OK] İşlem tamamlandı. Toplam {len(all_content)-1} içerik eklendi.")

if __name__ == "__main__":
    main()
