import requests
from bs4 import BeautifulSoup
import time

BASE_URL = "https://cizgimax.online"
CATEGORIES = {
    "Son Eklenenler": "?orderby=date&order=DESC",
    "Aile": "?s_type&tur[0]=aile&orderby=date&order=DESC",
    "Aksiyon": "?s_type&tur[0]=aksiyon-macera&orderby=date&order=DESC",
    "Animasyon": "?s_type&tur[0]=animasyon&orderby=date&order=DESC",
    "Bilim Kurgu": "?s_type&tur[0]=bilim-kurgu-fantazi&orderby=date&order=DESC",
    "Çocuklar": "?s_type&tur[0]=cocuklar&orderby=date&order=DESC",
    "Komedi": "?s_type&tur[0]=komedi&orderby=date&order=DESC"
}

M3U_FILENAME = "cizgimax.m3u"

def fetch_category(category_path, max_pages=3):
    """Belirtilen kategoriden içerikleri toplar."""
    items = []
    for page in range(1, max_pages+1):
        url = f"{BASE_URL}/diziler/page/{page}{category_path}"
        resp = requests.get(url)
        if resp.status_code != 200:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select("ul.filter-results li")
        if not results:
            break
        for r in results:
            title_el = r.select_one("h2.truncate")
            link_el = r.select_one("div.poster-subject a")
            poster_el = r.select_one("div.poster-media img")
            if title_el and link_el:
                items.append({
                    "title": title_el.text.strip(),
                    "url": BASE_URL + link_el.get("href"),
                    "poster": poster_el.get("data-src") if poster_el else "",
                })
    return items

def get_stream_link(content_url):
    """İçerik detay sayfasından stream iframe çıkartır."""
    resp = requests.get(content_url)
    soup = BeautifulSoup(resp.text, "html.parser")
    link_lis = soup.select("ul.linkler li")
    for li in link_lis:
        a_tag = li.select_one("a")
        if a_tag and "data-frame" in a_tag.attrs:
            return a_tag["data-frame"]
    # Not found
    return None

def generate_m3u(category_dict):
    lines = ["#EXTM3U"]
    for cat_name, contents in category_dict.items():
        for item in contents:
            stream_url = get_stream_link(item["url"])
            # Kategorileri group-title ile ayırıyoruz. tvg-logo da eklenebilir.
            extinf = f'#EXTINF:-1 group-title="{cat_name}" tvg-logo="{item["poster"]}",{item["title"]}'
            lines.append(extinf)
            lines.append(stream_url if stream_url else "")
    return "\n".join(lines)

def main():
    while True:
        category_data = {}
        for cat_name, cat_path in CATEGORIES.items():
            print(f"{cat_name} kategorisi çekiliyor...")
            contents = fetch_category(cat_path, max_pages=3)
            category_data[cat_name] = contents
        print("Stream linkleri bulunuyor ve m3u hazırlanıyor...")
        m3u_text = generate_m3u(category_data)
        with open(M3U_FILENAME, "w", encoding="utf-8") as f:
            f.write(m3u_text)
        print(f"{M3U_FILENAME} dosyasına yazıldı.")

        # Github otomasyonu için örnek:
        # os.system(f"git add {M3U_FILENAME} && git commit -m 'm3u güncellendi' && git push")
        print("20 dakika sonra tekrar güncellenecek...")
        time.sleep(20 * 60)

if __name__ == "__main__":
    main()
