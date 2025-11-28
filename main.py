import time
import re
import base64
import sys
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from seleniumbase import SB

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
# Tarayıcı olduğu için aynı anda 50 tane açamayız, tek tek ama kesin gider.
# Hızlandırmak için sadece en güncel sitemapleri alacağız.

# Hedef Sitemapler (En güncel içerikler)
TARGET_SITEMAPS = [
    "https://cizgimax.online/post-sitemap.xml",
    "https://cizgimax.online/post-sitemap2.xml",
    "https://cizgimax.online/post-sitemap3.xml"
]

FINAL_PLAYLIST = []

# --- KOTLIN PORTU: AES ŞİFRE ÇÖZÜCÜ ---
def decrypt_aes(encrypted, password):
    try:
        # Cloudstream AesHelper mantığı
        key = password.encode('utf-8')
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        iv = key[:16]
        
        encrypted_bytes = base64.b64decode(encrypted)
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        
        try: dec = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
        except: dec = cipher.decrypt(encrypted_bytes) # Padding yoksa
        
        return dec.decode('utf-8', 'ignore').strip().replace('\x00', '')
    except: return None

def extract_video_from_source(html_source):
    """Sayfa kaynağından video linkini (CizgiDuo/Sibnet/M3U8) çıkarır"""
    
    # 1. CizgiDuo (AES Şifreli)
    # Regex: bePlayer('pass', '{data}')
    be_match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html_source)
    if be_match:
        password = be_match.group(1)
        json_raw = be_match.group(2)
        data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
        if data_match:
            decrypted = decrypt_aes(data_match.group(1), password)
            if decrypted:
                m3u = re.search(r'video_location":"([^"]+)"', decrypted)
                if m3u: return m3u.group(1).replace("\\", "")

    # 2. Sibnet
    # Iframe içinde veya doğrudan kaynakta olabilir
    if "video.sibnet.ru" in html_source:
        slug_match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html_source)
        if slug_match:
            slug = slug_match.group(1)
            return f"https://video.sibnet.ru{slug}" if slug.startswith("/") else slug

    # 3. Genel M3U8/MP4
    m3u8 = re.search(r'(https?://[^"\'\s<>]+\.m3u8)', html_source)
    if m3u8: return m3u8.group(1).replace("\\", "")
    
    mp4 = re.search(r'(https?://[^"\'\s<>]+\.mp4)', html_source)
    if mp4: return mp4.group(1).replace("\\", "")

    return None

def main():
    print("CizgiMax Cloudflare-Killer Bot Başlatılıyor...")
    
    # SB (SeleniumBase) Context Manager
    # uc=True -> Undetected Chromedriver (Cloudflare'i geçer)
    # headless=True -> Arka planda çalışır (GitHub Actions için şart, ama xvfb ile)
    with SB(uc=True, headless=True) as sb:
        
        print("Tarayıcı başlatıldı. Siteye erişiliyor...")
        try:
            # Önce ana sayfaya git ki Cloudflare kontrolü geçsin ve cookie alalım
            sb.open(BASE_URL)
            sb.sleep(5) # Cloudflare kontrolü için bekle
            
            # Title kontrolü
            if "Just a moment" in sb.get_title():
                print("Cloudflare hala aktif, 5 saniye daha bekleniyor...")
                sb.sleep(5)
            
            print("Siteye erişim sağlandı!")
        except Exception as e:
            print(f"Siteye giriş hatası: {e}")
            return

        # --- SITEMAP TARAMA ---
        episode_links = []
        
        for sitemap in TARGET_SITEMAPS:
            print(f" > Sitemap okunuyor: {sitemap}")
            try:
                sb.open(sitemap)
                xml_content = sb.get_page_source()
                
                # XML Parse (Basit regex ile, daha hızlı)
                # <loc>https://cizgimax.online/dizi-adi-bolum-izle/</loc>
                urls = re.findall(r'<loc>(https://cizgimax\.online/[^<]+)</loc>', xml_content)
                
                count = 0
                for url in urls:
                    if "-izle" in url or "bolum" in url:
                        episode_links.append(url)
                        count += 1
                print(f"   + {count} bölüm eklendi.")
            except Exception as e:
                print(f"   Sitemap okuma hatası: {e}")

        print(f"\nToplam {len(episode_links)} bölüm işlenecek.")
        
        # --- BÖLÜMLERİ İŞLEME ---
        processed = 0
        for link in episode_links:
            try:
                sb.open(link)
                # Sayfanın yüklenmesini bekle (Element varlığı kontrolü gerekebilir ama sleep en basiti)
                # Çok hızlı gitmemek lazım
                
                # Başlık ve Poster
                soup = BeautifulSoup(sb.get_page_source(), 'html.parser')
                
                # Başlık
                h1 = soup.select_one("h1.page-title")
                full_title = h1.text.strip() if h1 else "Bilinmeyen Bölüm"
                full_title = full_title.replace("izle", "").replace("ÇizgiMax", "").strip()
                
                # Poster
                img = soup.select_one("img.series-profile-thumb")
                poster = img.get("src") if img else ""
                
                # Kategori (Dizi Adı)
                breadcrumb = soup.select("div.Breadcrumb span[itemprop='name']")
                category = "Genel"
                if len(breadcrumb) > 1:
                    category = breadcrumb[1].text.strip()

                # VİDEO ÇÖZME (Önemli Kısım)
                video_url = None
                
                # 1. Sayfa kaynağında direkt var mı? (CizgiDuo embed scripti)
                video_url = extract_video_from_source(sb.get_page_source())
                
                # 2. Eğer yoksa, iframe'lerin içine gir
                if not video_url:
                    # İframe linklerini bul
                    frames = soup.select("ul.linkler li a")
                    
                    # Sibnet ve CizgiDuo'yu öne al
                    frames = sorted(frames, key=lambda x: 0 if "sibnet" in str(x) or "cizgi" in str(x) else 1)
                    
                    for frame in frames:
                        src = frame.get("data-frame")
                        if not src: continue
                        if src.startswith("//"): src = "https:" + src
                        
                        # Iframe sayfasına git
                        try:
                            sb.open(src)
                            # Sibnet ise video sayfasına yönlendirilebilir, kaynak kodu al
                            src_html = sb.get_page_source()
                            video_url = extract_video_from_source(src_html)
                            if video_url: break
                        except: pass
                
                if video_url:
                    print(f" [+] {full_title}")
                    FINAL_PLAYLIST.append({
                        "group": category,
                        "title": full_title,
                        "logo": poster,
                        "url": video_url
                    })
                else:
                    pass 
                    # print(f" [-] Bulunamadı: {full_title}")

            except Exception as e:
                print(f"Hata ({link}): {e}")
            
            processed += 1
            if processed % 20 == 0:
                print(f"İlerleme: {processed}/{len(episode_links)}")

    # --- M3U KAYDET ---
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...")
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

if __name__ == "__main__":
    main()
