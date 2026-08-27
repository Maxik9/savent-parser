import requests
import re
import json
import logging
import html
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

BASE_URL = 'https://savent.ua'
UA_SITEMAP = f'{BASE_URL}/products_ua.xml'
OUTPUT_FILE = 'feed.xml'
MAX_WORKERS = 10

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7'
}

CATEGORIES = [
    {
        'id': '1',
        'slug': 'khimichni-zasoby-dlia-chyshchennia',
        'name_ua': 'Хімічні засоби для чищення димоходів та котлів',
        'name_ru': 'Химические средства для чистки дымоходов и котлов'
    },
    {
        'id': '2',
        'slug': 'shchitky-dlia-chyshchennia-dymokhodiv-ta-teploobminnykiv',
        'name_ua': 'Щітки для чищення димоходів та теплообмінників',
        'name_ru': 'Щетки для чистки дымоходов и теплообменников'
    },
    {
        'id': '3',
        'slug': 'ghnuchki-ruchky-dlia-chyshchennia',
        'name_ua': 'Гнучкі ручки та аксесуари для чищення димоходів',
        'name_ru': 'Гибкие ручки и аксессуары для чистки дымоходов'
    },
    {
        'id': '4',
        'slug': 'rotorna-chystka-dymokhodiv',
        'name_ua': 'Роторна чистка димоходів',
        'name_ru': 'Роторная чистка дымоходов'
    }
]

def get_category_id_by_url(url):
    for cat in CATEGORIES:
        if cat['slug'] in url:
            return cat['id']
    return '1'

def fetch_sitemap_urls(url, session):
    try:
        res = session.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        urls = re.findall(r'<loc>(.*?)</loc>', res.text)
        return [u.strip() for u in urls if u.strip()]
    except Exception as e:
        logger.error(f'Ошибка при получении sitemap {url}: {e}')
        return []

def clean_html_description(soup_elem):
    if not soup_elem:
        return ''
    for tag in soup_elem.find_all(['script', 'style', 'noscript', 'form', 'button', 'iframe']):
        tag.decompose()
    content = soup_elem.decode_contents().strip()
    return content

def parse_product_page(url, session, lang='ua'):
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Заголовок
        h1 = soup.find('h1')
        title = h1.text.strip() if h1 else ''
        
        # JSON-LD Product schema
        json_ld = {}
        for s in soup.find_all('script', type='application/ld+json'):
            if not s.string:
                continue
            try:
                data = json.loads(s.string.strip())
                if isinstance(data, dict) and data.get('@type') == 'Product':
                    json_ld = data
                    break
            except Exception:
                pass
                
        # Цена
        price_tag = soup.find('span', class_='active_prise')
        price = ''
        if price_tag:
            price = re.sub(r'[^\d.]', '', price_tag.text.replace(',', '.'))
        elif json_ld.get('offers', {}).get('price'):
            price = str(json_ld['offers']['price'])
            
        # Наличие
        status_tag = soup.find('div', class_='details_status')
        status_text = status_tag.text.lower() if status_tag else ''
        available = 'немає' not in status_text and 'нет в наличии' not in status_text and 'out of stock' not in status_text
        if json_ld.get('offers', {}).get('availability'):
            if 'OutOfStock' in json_ld['offers']['availability']:
                available = False
                
        # Изображения (все фото)
        images = []
        if json_ld.get('image'):
            img_val = json_ld['image']
            if isinstance(img_val, list):
                images.extend([img for img in img_val if isinstance(img, str)])
            elif isinstance(img_val, str):
                images.append(img_val)
                
        # Дополнительно проверяем галерею на странице
        for img in soup.find_all('img', class_='gallery-item'):
            src = img.get('data-src') or img.get('src')
            if src and src.startswith('http') and src not in images:
                images.append(src)
                
        for div in soup.find_all('div', class_='sub_photo'):
            src = div.get('data-src')
            if src:
                full_src = src.replace('/t_', '/')
                if full_src not in images and full_src.startswith('http'):
                    images.append(full_src)
                    
        # Описание
        desc_div = soup.find('div', class_='article_text')
        description = clean_html_description(desc_div)
        
        # Характеристики
        params = {}
        vendor = 'Savent'
        vendor_code = json_ld.get('mpn', '')
        
        btn_prod = soup.find(attrs={'data-product-id': True})
        prod_id = btn_prod['data-product-id'] if btn_prod else (vendor_code or '')
        
        char_table = soup.find('div', class_='char_table')
        if char_table:
            for row in char_table.find_all('div', class_='ct_row'):
                cols = [c.text.strip() for c in row.find_all('div', class_='ct_col')]
                if len(cols) == 2 and cols[0] and cols[1]:
                    param_name = cols[0].rstrip(':').strip()
                    param_val = cols[1].strip()
                    if param_name.lower() in ['виробник', 'производитель', 'бренд', 'торгова марка']:
                        vendor = param_val
                    elif param_name.lower() in ['артикул', 'код товару', 'код товара']:
                        vendor_code = param_val
                    elif param_name.lower() not in ['категорія', 'категория']:
                        params[param_name] = param_val

        # Если артикул так и не найден, используем MPN или ID
        if not vendor_code:
            vendor_code = prod_id
            
        # Ссылка на альтернативный язык
        ru_url = None
        ru_tag = soup.find('a', href=lambda h: h and '/ru/' in h and ('khim' in h or 'shchit' in h or 'ghnuch' in h or 'rotor' in h))
        if ru_tag and ru_tag.get('href'):
            ru_url = ru_tag['href']
            
        return {
            'url': url,
            'title': title,
            'price': price,
            'available': available,
            'images': images,
            'description': description,
            'params': params,
            'vendor': vendor,
            'vendor_code': vendor_code,
            'prod_id': prod_id,
            'category_id': get_category_id_by_url(url),
            'ru_url': ru_url
        }
    except Exception as e:
        logger.error(f'Ошибка при парсинге {url}: {e}')
        return None

def fetch_all_products():
    session = requests.Session()
    logger.info('Получение карты сайта UA...')
    ua_urls = fetch_sitemap_urls(UA_SITEMAP, session)
    logger.info(f'Найдено {len(ua_urls)} товаров в UA sitemap')
    
    products = []
    logger.info('Парсинг UA страниц товаров...')
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {executor.submit(parse_product_page, url, session, 'ua'): url for url in ua_urls}
        for future in as_completed(future_to_url):
            data = future.result()
            if data:
                products.append(data)
                
    logger.info(f'Успешно спарсено {len(products)} UA карточек.')
    
    # Теперь парсим RU версии для получения русских названий и описаний
    logger.info('Парсинг RU версий товаров...')
    ru_tasks = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for p in products:
            if p.get('ru_url'):
                ru_tasks[executor.submit(parse_product_page, p['ru_url'], session, 'ru')] = p
                
        for future in as_completed(ru_tasks):
            p = ru_tasks[future]
            ru_data = future.result()
            if ru_data:
                p['title_ru'] = ru_data.get('title', '')
                p['description_ru'] = ru_data.get('description', '')
                p['params_ru'] = ru_data.get('params', {})
            else:
                p['title_ru'] = p.get('title', '')
                p['description_ru'] = p.get('description', '')
                p['params_ru'] = p.get('params', {})
                
    # Сортируем по category_id и prod_id для стабильного порядка
    products.sort(key=lambda x: (x.get('category_id', '0'), str(x.get('prod_id', ''))))
    return products

def generate_prom_xml(products, output_path=OUTPUT_FILE):
    logger.info(f'Генерация YML XML для Prom.ua ({output_path})...')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    xml_lines = []
    xml_lines.append('<?xml version="1.0" encoding="utf-8"?>')
    xml_lines.append('<!DOCTYPE yml_catalog SYSTEM "shops.dtd">')
    xml_lines.append(f'<yml_catalog date="{now_str}">')
    xml_lines.append('  <shop>')
    xml_lines.append('    <name>Savent</name>')
    xml_lines.append('    <company>Savent</company>')
    xml_lines.append('    <url>https://savent.ua/</url>')
    xml_lines.append('    <currencies>')
    xml_lines.append('      <currency id="UAH" rate="1"/>')
    xml_lines.append('    </currencies>')
    xml_lines.append('    <categories>')
    for cat in CATEGORIES:
        cid = cat['id']
        cname = html.escape(cat['name_ua'])
        xml_lines.append(f'      <category id="{cid}">{cname}</category>')
    xml_lines.append('    </categories>')
    xml_lines.append('    <offers>')
    
    for p in products:
        offer_id = p.get('prod_id') or p.get('vendor_code') or str(hash(p['url']))
        avail_str = 'true' if p.get('available') else 'false'
        
        name_ua = p.get('title', '')
        name_ru = p.get('title_ru') or name_ua
        
        desc_ua = p.get('description', '')
        desc_ru = p.get('description_ru') or desc_ua
        
        price = p.get('price', '0')
        cat_id = p.get('category_id', '1')
        vendor = p.get('vendor', 'Savent')
        vendor_code = p.get('vendor_code', offer_id)
        
        xml_lines.append(f'      <offer id="{html.escape(str(offer_id))}" available="{avail_str}">')
        xml_lines.append(f'        <url>{html.escape(p["url"])}</url>')
        xml_lines.append(f'        <price>{price}</price>')
        xml_lines.append('        <currencyId>UAH</currencyId>')
        xml_lines.append(f'        <categoryId>{cat_id}</categoryId>')
        
        # Все фото товара
        for img in p.get('images', []):
            xml_lines.append(f'        <picture>{html.escape(img)}</picture>')
            
        xml_lines.append(f'        <vendor>{html.escape(vendor)}</vendor>')
        xml_lines.append(f'        <vendorCode>{html.escape(str(vendor_code))}</vendorCode>')
        
        # Названия
        xml_lines.append(f'        <name>{html.escape(name_ru)}</name>')
        xml_lines.append(f'        <name_ua>{html.escape(name_ua)}</name_ua>')
        
        # Описания в блоках CDATA
        xml_lines.append(f'        <description><![CDATA[{desc_ru}]]></description>')
        xml_lines.append(f'        <description_ua><![CDATA[{desc_ua}]]></description_ua>')
        
        # Характеристики
        params = p.get('params_ru') if p.get('params_ru') else p.get('params', {})
        for pname, pval in params.items():
            xml_lines.append(f'        <param name="{html.escape(pname)}">{html.escape(str(pval))}</param>')
            
        xml_lines.append('      </offer>')
        
    xml_lines.append('    </offers>')
    xml_lines.append('  </shop>')
    xml_lines.append('</yml_catalog>')
    
    full_xml = '\n'.join(xml_lines)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(full_xml)
        
    logger.info(f'Файл успешно записан: {output_path} ({len(products)} товаров).')

if __name__ == '__main__':
    start = time.time()
    products = fetch_all_products()
    generate_prom_xml(products)
    logger.info(f'Парсинг завершен за {time.time() - start:.2f} сек.')
