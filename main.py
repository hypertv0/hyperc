import cloudscraper
from bs4 import BeautifulSoup
import re
import time
import json
import base64
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import sys

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
MAX_PAGES = 3  # Her kategoriden kaç sayfa?

# Kategoriler
CATEGORIES = {
    "Son Eklenenler": f"{BASE_URL}/diziler/?orderby=date&order=DESC",
    "Aile": f"{BASE_URL}/diziler/?s_type&tur[0]=aile&orderby=date&order=DESC",
    "Aksiyon": f"{BASE_URL}/diziler/?s_type&tur[0]=aksiyon-macera&orderby=date&order=DESC",
    "Animasyon": f"{BASE_URL}/diziler/?s_type&tur[0]=animasyon&orderby=date&order=DESC",
    "Bilim Kurgu": f"{BASE_URL}/diziler/?s_type&tur[0]=bilim-kurgu-fantazi&orderby=date&order=DESC",
    "Çocuklar": f"{BASE_URL}/diziler/?s_type&tur[0]=cocuklar&orderby=date&order=DESC",
    "Komedi": f"{BASE_URL}/diziler/?s_type&tur[0]=komedi&orderby=date&order=DESC"
}

# CloudScraper Nesnesi (CloudflareKiller)
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

FINAL_PLAYLIST = []

def decrypt_cizgiduo(encrypted_data, password):
    """
    Kotlin Kodundaki CizgiDuo AES Şifre Çözme Mantığı
    AesHelper.cryptoAESHandler(bePlayerData, bePlayerPass.toByteArray(), false)
    """
    try:
        # Kotlin kodu AES/CBC/PKCS5Padding kullanıyor genelde
        # Password key ve IV olarak kullanılıyor olabilir veya OpenSSL formatıdır.
        # Genellikle bu tür JS playerlarda Key = Password, IV = Password (veya türevi)
        
        key = password.encode('utf-8')
        # Anahtar uzunluğu 16, 24 veya 32 byte olmalı. Eksikse tamamla, fazlaysa kes.
        if len(key) > 32: key = key[:32]
        elif len(key) in [16, 24, 32]: pass
        else: key = key.ljust(32, b'\0') # Basit padding

        # Şifreli veri Base64 decode
        encrypted_bytes = base64.b64decode(encrypted_data)
        
        # IV, şifreli verinin genelde başında gelmezse, key'in kendisi olabilir
        # Ancak OpenSSL formatında "Salted__" headerı yoksa genelde IV = Key'dir web playerlarda.
        # Cloudstream AESHelper kaynak koduna bakıldığında IV genelde parametre geçilmezse boş byte array veya key olabilir.
        
        # Deneme 1: IV = Key (Yaygın yöntem)
        cipher = AES.new(key, AES.MODE_CBC, iv=key[:16])
        decrypted = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
        return decrypted.decode('utf-8')
        
    except Exception as e:
        # print(f"AES Decrypt Hatası: {e}")
        return None

def get_source_cizgiduo(url, referer):
    """CizgiDuo Kaynağını Çözer"""
    try:
        html = scraper.get(url, headers={"Referer": referer}).text
        
        # Regex: bePlayer('pass', '{json_data}')
        match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
        if match:
            password = match.group(1)
            json_data_enc = match.group(2)
            
            # İçindeki "data" alanını bul
            # Genellikle json_data_enc şu formattadır: { "data": "sifreli_metin", ... }
            try:
                # Basit json parse (tırnak işaretlerine dikkat)
                enc_text_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_data_enc)
                if enc_text_match:
                    encrypted_text = enc_text_match.group(1)
                    decrypted_json = decrypt_cizgiduo(encrypted_text, password)
                    
                    if decrypted_json:
                        # İçinden video_location al
                        # video_location":"https:\/\/..."
                        m3u_match = re.search(r'video_location":"([^"]+)"', decrypted_json)
                        if m3u_match:
                            m3u_link = m3u_match.group(1).replace("\\", "")
                            return m3u_link
            except:
                pass
    except Exception as e:
        print(f"CizgiDuo Hatası: {e}")
    return None

def get_source_sibnet(url, referer):
    """Sibnet Kaynağını Çözer"""
    try:
        # ID'yi URL'den çek
        video_id = None
        if "videoid=" in url:
            video_id = re.search(r'videoid=(\d+)', url).group(1)
        elif "/video/" in url:
            video_id = re.search(r'/video/(\d+)', url).group(1)
            
        if video_id:
            real_url = f"https://video.sibnet.ru/video/{video_id}"
            html = scraper.get(real_url, headers={"Referer": referer}).text
            
            slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
            if slug:
                final_slug = slug.group(1)
                if final_slug.startswith("/"): return f"https://video.sibnet.ru{final_slug}"
                return final_slug
    except:
        pass
    return None

def process_episode(category, series_title, ep_title, ep_url, poster):
    try:
        # Cloudscraper ile bölüm sayfasına git
        resp = scraper.get(ep_url, headers={"Referer": BASE_URL})
        if resp.status_code != 200: return

        soup = BeautifulSoup(resp.text, 'html.parser')
        links = soup.select("ul.linkler li a")
        
        found_url = None
        
        # Öncelik Sırası: CizgiDuo (Kaliteli M3U8) -> Sibnet -> Diğerleri
        # Linkleri sırala
        sorted_links = sorted(links, key=lambda x: (
            2 if "cizgiduo" in str(x.get("data-frame")) else
            1 if "sibnet" in str(x.get("data-frame")) else 0
        ), reverse=True)

        for link in sorted_links:
            iframe_src = link.get("data-frame")
            if not iframe_src: continue
            
            if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src
            
            # --- KAYNAK ÇÖZME ---
            if "cizgiduo" in iframe_src or "cizgipass" in iframe_src:
                found_url = get_source_cizgiduo(iframe_src, ep_url)
            
            elif "sibnet" in iframe_src:
                found_url = get_source_sibnet(iframe_src, ep_url)
            
            # Eğer yukarıdaki fonksiyonlar bulamazsa veya başka kaynaksa m3u8 ara
            if not found_url:
                try:
                    src_html = scraper.get(iframe_src, headers={"Referer": ep_url}).text
                    m3u8 = re.search(r'(https?://[^"\']+\.m3u8)', src_html)
                    if m3u8: found_url = m3u8.group(1)
                except: pass

            if found_url: break
        
        if found_url:
            clean_ep = ep_title.replace(series_title, "").strip()
            if not clean_ep: clean_ep = ep_title
            
            full_title = f"{series_title} - {clean_ep}"
            
            # Kategori + Başlık ID'si (Gruplama için)
            # HTML tarafında doğru çalışması için temiz başlık gönderiyoruz
            
            FINAL_PLAYLIST.append({
                "group": category,
                "title": full_title,
                "logo": poster,
                "url": found_url
            })
            print(f"  [+] Eklendi: {full_title}")
        else:
            print(f"  [-] Video Yok: {ep_title}")
            
    except Exception as e:
        print(f"Bölüm İşleme Hatası: {e}")

def process_series(category, series_title, series_url, poster):
    print(f" > Dizi Taranıyor: {series_title}")
    try:
        resp = scraper.get(series_url)
        if resp.status_code != 200: return
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        episodes_divs = soup.select("div.asisotope div.ajax_post")
        
        for ep_div in episodes_divs:
            name_span = ep_div.select_one("span.episode-names")
            if not name_span: continue
            ep_name = name_span.text.strip()
            
            link_tag = ep_div.select_one("a")
            if not link_tag: continue
            ep_link = link_tag['href']
            
            process_episode(category, series_title, ep_name, ep_link, poster)
            time.sleep(0.5) # Cloudflare kızmasın diye hafif bekleme
            
    except Exception as e:
        print(f"Dizi Hatası: {e}")

def scan_category(cat_name, cat_url):
    print(f"\n--- Kategori: {cat_name} ---")
    
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = cat_url
        else:
            if "/?p=" in cat_url:
                parts = cat_url.split("/?p=")
                url = f"{parts[0]}/page/{page}/?p={parts[1]}"
            else:
                parts = cat_url.split("/?")
                base = parts[0]
                query = "/?" + parts[1] if len(parts) > 1 else ""
                if base.endswith("/"): base = base[:-1]
                url = f"{base}/page/{page}{query}"

        print(f" >> Sayfa {page}...")
        
        try:
            resp = scraper.get(url)
            # Cloudflare kontrolü
            if "Just a moment" in resp.text:
                print("!!! Cloudflare Takıldı (Tekrar Deneniyor) !!!")
                time.sleep(5)
                resp = scraper.get(url) # Tekrar dene
            
            if resp.status_code != 200: 
                print(f"Erişim Hatası: {resp.status_code}")
                break
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            items = soup.select("ul.filter-results li")
            
            if not items:
                print("   -> İçerik bitti.")
                break
                
            for item in items:
                title_tag = item.select_one("h2.truncate")
                if not title_tag: continue
                title = title_tag.text.strip()
                
                link_tag = item.select_one("div.poster-subject a")
                if not link_tag: continue
                link = link_tag['href']
                
                img_tag = item.select_one("div.poster-media img")
                poster = img_tag.get("data-src") if img_tag else ""
                
                process_series(cat_name, title, link, poster)
                
        except Exception as e:
            print(f"Kategori Hatası: {e}")
            break

def main():
    print("CizgiMax Cloudstream Portu Başlatılıyor...")
    start_time = time.time()
    
    for c_name, c_url in CATEGORIES.items():
        scan_category(c_name, c_url)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik. Kaydediliyor...")
    
    # Sıralama
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Bitti! Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    main()
