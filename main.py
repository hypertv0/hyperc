import time
import re
import base64
import sys
import random
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from seleniumbase import SB

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
# Kaç sayfa taranacak? (Sitede 50+ sayfa var. Hepsini istersen 60 yap ama saatler sürer)
MAX_PAGES_TO_SCAN = 5 
# Her diziye girip bölümleri tarayacak.

FINAL_PLAYLIST = []

# --- AES DECRYPTOR (Cloudstream: CizgiDuo.kt) ---
def decrypt_aes(encrypted, password):
    try:
        key = password.encode('utf-8')
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        iv = key[:16]
        
        encrypted_bytes = base64.b64decode(encrypted)
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        
        try:
            decrypted = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
        except ValueError:
            decrypted = cipher.decrypt(encrypted_bytes)
            
        return decrypted.decode('utf-8', errors='ignore').strip().replace('\x00', '')
    except:
        return None

# --- VİDEO KAYNAĞI ÇÖZÜCÜ ---
def extract_video_source(sb, html_content, referer_url):
    """Sayfa kaynağındaki video linkini bulur (Sibnet/CizgiDuo/M3U8)"""
    
    # 1. CizgiDuo (Şifreli)
    # Regex: bePlayer('pass', '{data}')
    match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html_content)
    if match:
        password = match.group(1)
        json_raw = match.group(2)
        data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
        if data_match:
            dec = decrypt_aes(data_match.group(1), password)
            if dec:
                m3u = re.search(r'video_location":"([^"]+)"', dec)
                if m3u: return m3u.group(1).replace("\\", "")

    # 2. Sibnet (ID varsa)
    # Iframe içinde veya script içinde olabilir
    if "video.sibnet.ru" in html_content:
        # Önce ID'yi bul
        vid_match = re.search(r'(?:videoid=|/video/)(\d+)', html_content)
        if vid_match:
            vid_id = vid_match.group(1)
            real_url = f"https://video.sibnet.ru/video/{vid_id}"
            
            # Sibnet sayfasına gitmemiz gerekebilir (Kaynak kodda yoksa)
            # Ancak hız için önce mevcut HTML'de arayalım
            slug_match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html_content)
            if slug_match:
                slug = slug_match.group(1)
                return f"https://video.sibnet.ru{slug}" if slug.startswith("/") else slug
            
            # Mevcut HTML'de yoksa, Sibnet sayfasına git (Maliyetli işlem)
            try:
                sb.open(real_url)
                time.sleep(1) # Yüklenmesini bekle
                sib_html = sb.get_page_source()
                slug_match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', sib_html)
                if slug_match:
                    slug = slug_match.group(1)
                    return f"https://video.sibnet.ru{slug}" if slug.startswith("/") else slug
            except: pass

    # 3. Genel Tarama (.m3u8 / .mp4)
    m3u8 = re.search(r'(https?://[^"\'\s<>]+\.m3u8)', html_content)
    if m3u8: return m3u8.group(1).replace("\\", "")
    
    mp4 = re.search(r'(https?://[^"\'\s<>]+\.mp4)', html_content)
    if mp4: return mp4.group(1).replace("\\", "")

    return None

def main():
    print("CizgiMax Cloudstream-Logic Bot Başlatılıyor...")
    
    # SeleniumBase'i başlat (UC Mode: Cloudflare'i geçer)
    with SB(uc=True, headless=True) as sb:
        
        # 1. Ana Sayfaya Gir (Cloudflare Check)
        try:
            print("Siteye giriş yapılıyor...")
            sb.open(BASE_URL)
            time.sleep(6) # Cloudflare kontrolü için bekle
            if "Just a moment" in sb.get_title():
                print("Cloudflare bekleniyor...")
                time.sleep(10)
        except Exception as e:
            print(f"Giriş hatası: {e}")
            return

        # 2. Sayfaları Gez (Pagination)
        # Cloudstream mantığı: /diziler/page/1/, /diziler/page/2/ ...
        
        for page in range(1, MAX_PAGES_TO_SCAN + 1):
            page_url = f"{BASE_URL}/diziler/page/{page}/?orderby=date&order=DESC"
            print(f"\n>>> Sayfa {page} taranıyor: {page_url}")
            
            try:
                sb.open(page_url)
                time.sleep(3) # Sayfanın yüklenmesini bekle
                
                soup = BeautifulSoup(sb.get_page_source(), 'html.parser')
                
                # Dizileri Bul (Cloudstream: ul.filter-results li)
                items = soup.select("ul.filter-results li")
                
                if not items:
                    print("Bu sayfada içerik bulunamadı veya Cloudflare engeli.")
                    continue
                
                # Bu sayfadaki dizi linklerini topla
                series_queue = []
                for item in items:
                    link_tag = item.select_one("div.poster-subject a")
                    if link_tag:
                        series_queue.append(link_tag['href'])
                
                print(f"   > {len(series_queue)} dizi bulundu. İşleniyor...")
                
                # 3. Dizi Detayına Git ve Bölümleri Al
                for series_url in series_queue:
                    try:
                        sb.open(series_url)
                        # time.sleep(2) # Dizi sayfasının yüklenmesi
                        
                        series_soup = BeautifulSoup(sb.get_page_source(), 'html.parser')
                        
                        # Dizi Başlığı
                        h1 = series_soup.select_one("h1.page-title")
                        series_title = h1.text.strip() if h1 else "Bilinmeyen Dizi"
                        
                        # Kategori (Breadcrumb veya Etiket)
                        category = "Genel"
                        crumbs = series_soup.select("div.Breadcrumb span[itemprop='name']")
                        if len(crumbs) > 1: category = crumbs[1].text.strip()
                        
                        # Poster
                        img = series_soup.select_one("img.series-profile-thumb")
                        poster = img.get("src") if img else ""
                        
                        # Bölümleri Bul (Cloudstream: div.asisotope div.ajax_post)
                        # Ancak gönderdiğin HTML'de "swiper-slide ss-episode" yapısı var.
                        # Kod her ikisini de denesin.
                        
                        episodes = []
                        
                        # Yöntem A: Ajax Post (Cloudstream Eklentisi)
                        ep_divs_a = series_soup.select("div.asisotope div.ajax_post a")
                        for ep in ep_divs_a:
                            episodes.append(ep['href'])
                            
                        # Yöntem B: Swiper Slide (HTML Çıktısı)
                        if not episodes:
                            ep_divs_b = series_soup.select(".swiper-slide a.episode-link")
                            for ep in ep_divs_b:
                                episodes.append(ep['href'])
                        
                        # Tekrarları temizle
                        episodes = list(set(episodes))
                        
                        print(f"   > {series_title}: {len(episodes)} bölüm bulundu.")
                        
                        # 4. Bölüm Sayfasına Git ve Videoyu Çöz
                        for ep_url in episodes:
                            try:
                                sb.open(ep_url)
                                # time.sleep(1) # Bölüm sayfası
                                
                                ep_soup = BeautifulSoup(sb.get_page_source(), 'html.parser')
                                
                                # Başlık Düzenleme
                                # Başlık genelde: "Gumball ... 1. Bölüm"
                                ep_h1 = ep_soup.select_one("h1.page-title")
                                full_title = ep_h1.text.replace("izle", "").replace("ÇizgiMax", "").strip() if ep_h1 else f"{series_title} - Bölüm"
                                
                                # İframe Linklerini Bul
                                # Cloudstream: ul.linkler li a[data-frame]
                                links = ep_soup.select("ul.linkler li a")
                                
                                # Sibnet'i öne al
                                links = sorted(links, key=lambda x: 0 if "sibnet" in str(x.get("data-frame")) else 1)
                                
                                found_video = None
                                for link in links:
                                    iframe_src = link.get("data-frame")
                                    if not iframe_src: continue
                                    
                                    # İframe'e gitmeden önce URL düzelt
                                    if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src
                                    
                                    # Iframe'i açmaya çalış (SB ile)
                                    try:
                                        sb.open(iframe_src)
                                        # time.sleep(0.5)
                                        found_video = extract_video_source(sb, sb.get_page_source(), ep_url)
                                        if found_video: break
                                    except: pass
                                
                                if found_video:
                                    print(f"     [+] Eklendi: {full_title}")
                                    FINAL_PLAYLIST.append({
                                        "group": category,
                                        "title": full_title,
                                        "logo": poster,
                                        "url": found_video
                                    })
                                
                            except Exception as e:
                                print(f"     [-] Bölüm hatası: {e}")
                                
                    except Exception as e:
                        print(f"   [-] Dizi hatası: {e}")

            except Exception as e:
                print(f"Sayfa hatası: {e}")

    # --- KAYDET ---
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik. Kaydediliyor...")
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')
            
    print("İşlem Tamamlandı.")

if __name__ == "__main__":
    main()
