# analysis_engine.py
# Tüm veri analizi ve raporlama fonksiyonlarını içerir.

import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from datetime import datetime, timedelta
from database import db, Urun, SatisKaydi  # Veritabanı modellerini database.py dosyasından alır
import warnings
import json
from sqlalchemy import func

def _get_daily_sales_data(urun_id):
    """
    Yardımcı fonksiyon: Belirli bir ürün için satış verilerini veritabanından çeker
    ve günlük olarak gruplar. Fiyat elastikiyeti hesaplaması için temel veriyi sağlar.
    """
    # İlgili ürüne ait tüm satış kayıtlarını çek
    query = db.session.query(
        SatisKaydi.tarih, 
        SatisKaydi.adet, 
        SatisKaydi.hesaplanan_birim_fiyat
    ).filter(SatisKaydi.urun_id == urun_id)
    
    satislar = query.all()
    
    # Modelin çalışması için en az 2 farklı veri noktası gerekir
    if not satislar or len(satislar) < 2:
        return None

    # Veriyi bir Pandas DataFrame'e dönüştür
    df_satislar = pd.DataFrame(satislar, columns=['tarih', 'adet', 'hesaplanan_birim_fiyat'])
    df_satislar['tarih'] = pd.to_datetime(df_satislar['tarih'])
    
    # Satışları fiyata göre gruplayıp günlük ortalama adedi hesapla
    # Bu, aynı gün içinde farklı fiyatlar (örn. indirim) varsa modelin bozulmasını engeller
    df_grouped = df_satislar.groupby('hesaplanan_birim_fiyat').agg(
        toplam_adet=('adet', 'sum'),
        gun_sayisi=('tarih', 'nunique') # Bu fiyattan kaç farklı günde satış yapıldığı
    ).reset_index()
    
    # Günlük ortalama satışı hesapla
    df_grouped['ortalama_gunluk_adet'] = df_grouped['toplam_adet'] / df_grouped['gun_sayisi']
    
    # Fiyat esnekliği hesaplaması için en az 2 farklı fiyat noktası gerekir
    if len(df_grouped) < 2:
        return None
        
    return df_grouped

def _generate_price_curve_data(model, maliyet, df_gunluk, simule_fiyat=None):
    """Optimizasyon ve simülasyon için grafik verisi hazırlar."""
    mevcut_fiyat = df_gunluk['ortalama_fiyat'].mean()
    
    # Grafik için X ekseni (fiyat aralığı) belirle
    min_fiyat = max(maliyet * 1.1, df_gunluk['ortalama_fiyat'].min() * 0.8) 
    max_fiyat = df_gunluk['ortalama_fiyat'].max() * 1.5
    
    # Eğer simüle edilen fiyat grafiğin dışında kalıyorsa, grafiği genişlet
    if simule_fiyat:
        fiyat_max = max(fiyat_max, simule_fiyat * 1.2)
        
    price_points = np.linspace(min_fiyat, max_fiyat, 50) # 50 noktalı bir eğri oluştur
    
    # Modeli kullanarak her fiyat noktası için talebi (satış adedini) tahmin et
    if model:
        predicted_demand = model.predict(price_points.reshape(-1, 1))
    else:
        # Model yoksa (tek fiyat), talebi sabit varsay (bu durumda grafik çok anlamlı olmaz)
        predicted_demand = np.full_like(price_points, df_gunluk['toplam_adet'].mean())

    predicted_demand[predicted_demand < 0] = 0 # Tahmini adet negatif olamaz
    
    # Her fiyat noktası için kârı hesapla: (Fiyat - Maliyet) * Tahmini Adet
    profit_points = (price_points - maliyet) * predicted_demand
    
    # Chart.js'in anlayacağı JSON formatına dönüştür
    chart_data = {
        'type': 'line', # Grafik tipi
        'labels': [round(p, 2) for p in price_points],
        'datasets': [{
            'label': 'Tahmini Günlük Kâr (TL)',
            'data': [round(p, 2) for p in profit_points],
            'borderColor': '#0d6efd', # Bootstrap Primary Rengi
            'backgroundColor': 'rgba(13, 110, 253, 0.2)',
            'fill': True,
            'tension': 0.4 # Eğriyi yumuşat
        }]
    }
    return json.dumps(chart_data)

# --- ANA ANALİZ MOTORLARI ---

def hesapla_hedef_marj(urun_ismi, hedef_marj_yuzdesi):
    """Motor 1: Hedef Marj Hesaplayıcı"""
    try:
        urun = Urun.query.filter_by(isim=urun_ismi).first()
        if not urun:
            return False, f"HATA: '{urun_ismi}' adında bir ürün bulunamadı.", None
        
        maliyet = urun.hesaplanan_maliyet
        if maliyet is None or maliyet <= 0:
            return False, f"HATA: '{urun_ismi}' ürününün maliyeti (0 TL) hesaplanmamış. Lütfen 'Menü Yönetimi'nden reçete ekleyin.", None
        
        if not (0 < hedef_marj_yuzdesi < 100):
            return False, "HATA: Hedef Marj %1 ile %99 arasında bir değer olmalıdır.", None

        marj_orani = hedef_marj_yuzdesi / 100.0
        gereken_satis_fiyati = maliyet / (1 - marj_orani)
        
        rapor = (
            f"--- HESAPLAMA SONUCU ---\n\n"
            f"  Ürün: {urun.isim}\n"
            f"  Hesaplanan Güncel Maliyet (COGS): {maliyet:.2f} TL\n"
            f"  İstenen Kar Marjı: %{hedef_marj_yuzdesi:.0f}\n\n"
            f"  🎯 GEREKEN SATIŞ FİYATI: {gereken_satis_fiyati:.2f} TL 🎯"
        )
        # Bu analiz grafik döndürmez
        return True, rapor, None
    
    except Exception as e:
        return False, f"Hesaplama hatası: {e}", None


def simule_et_fiyat_degisikligi(urun_ismi, test_edilecek_yeni_fiyat):
    """Motor 2: Fiyat Simülatörü"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        
        try:
            urun = Urun.query.filter_by(isim=urun_ismi).first()
            if not urun:
                return False, f"HATA: '{urun_ismi}' adında bir ürün bulunamadı.", None
            
            maliyet = urun.hesaplanan_maliyet
            if maliyet is None or maliyet <= 0:
                return False, f"HATA: '{urun_ismi}' ürününün maliyeti (0 TL) hesaplanmamış. Lütfen önce reçete ve hammadde fiyatlarını girin.", None

            df_gunluk = _get_daily_sales_data(urun.id)
            
            if df_gunluk is None or df_gunluk.empty:
                return False, f"HATA: '{urun_ismi}' için analiz edilecek yeterli satış verisi bulunamadı. (En az 2 farklı günde/fiyatta satış yapılmalı).", None
            
            # Ağırlıklı ortalama fiyatı hesapla
            mevcut_ortalama_fiyat = (df_gunluk['ortalama_fiyat'] * df_gunluk['toplam_adet']).sum() / df_gunluk['toplam_adet'].sum()
            mevcut_gunluk_satis = df_gunluk['toplam_adet'].mean()
            mevcut_gunluk_kar = (mevcut_ortalama_fiyat - maliyet) * mevcut_gunluk_satis

            rapor = (
                f"--- MEVCUT DURUM (Geçmiş Veri Ortalaması) ---\n"
                f"  Ortalama Fiyat: {mevcut_ortalama_fiyat:.2f} TL\n"
                f"  Ortalama Günlük Satış: {mevcut_gunluk_satis:.1f} adet\n"
                f"  Ürün Maliyeti: {maliyet:.2f} TL\n"
                f"  Tahmini Günlük Kar: {mevcut_gunluk_kar:.2f} TL\n"
                f"{'-'*50}\n"
            )
            
            if df_gunluk['ortalama_fiyat'].nunique() < 2:
                rapor += "UYARI: Ürün hep aynı fiyattan satılmış. Talep modeli kurulamaz.\nSimülasyon iptal edildi."
                return False, rapor, None

            # Fiyat ve Talep (Adet) arasındaki ilişkiyi modelle
            X = df_gunluk[['ortalama_fiyat']]
            y = df_gunluk['toplam_adet']
            model = LinearRegression().fit(X, y)
            
            if model.coef_[0] >= 0:
                rapor += "UYARI: Model, fiyat arttıkça satışların ARTTIĞINI söylüyor! Veri yetersiz veya hatalı (örn: enflasyonist ortam).\n"
                return False, rapor, None

            tahmini_yeni_satis = model.predict(np.array([[test_edilecek_yeni_fiyat]]))[0]
            tahmini_yeni_satis = max(0, tahmini_yeni_satis) # Negatif satış olamaz
            tahmini_yeni_kar = (test_edilecek_yeni_fiyat - maliyet) * tahmini_yeni_satis
            kar_degisimi = tahmini_yeni_kar - mevcut_gunluk_kar
            
            rapor += (
                f"--- SİMÜLASYON SONUCU ({test_edilecek_yeni_fiyat:.2f} TL) ---\n"
                f"  Tahmini Günlük Satış: {tahmini_yeni_satis:.1f} adet\n"
                f"  Tahmini Günlük Kar: {tahmini_yeni_kar:.2f} TL\n"
                f"{'='*50}\n"
            )
            
            if kar_degisimi > 0:
                rapor += f"  SONUÇ (TAVSİYE): BAŞARILI!\n  Günlük karınızı TAHMİNİ {kar_degisimi:.2f} TL ARTIRABİLİR."
            else:
                rapor += f"  SONUÇ (UYARI): BAŞARISIZ!\n  Günlük karınızı TAHMİNİ {abs(kar_degisimi):.2f} TL AZALTABİLİR."
            
            # Grafik verisini oluştur
            chart_data = _generate_price_curve_data(model, maliyet, df_gunluk, simule_fiyat=test_edilecek_yeni_fiyat)
            return True, rapor, chart_data
        
        except Exception as e:
            return False, f"Simülasyon hatası: {e}", None


def bul_optimum_fiyat(urun_ismi, fiyat_deneme_araligi=1.0):
    """Motor 3: Optimum Fiyat Motoru"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        
        try:
            urun = Urun.query.filter_by(isim=urun_ismi).first()
            if not urun:
                return False, f"HATA: '{urun_ismi}' adında bir ürün bulunamadı.", None
            
            maliyet = urun.hesaplanan_maliyet
            mevcut_fiyat = urun.mevcut_satis_fiyati
            if maliyet is None or maliyet <= 0:
                return False, f"HATA: '{urun_ismi}' ürününün maliyeti (0 TL) hesaplanmamış. Lütfen 'Menü Yönetimi'nden reçete ekleyin.", None
                
            df_gunluk = _get_daily_sales_data(urun.id)
            if df_gunluk is None or df_gunluk.empty:
                return False, f"HATA: '{urun_ismi}' için analiz edilecek yeterli satış verisi bulunamadı. (En az 2 farklı günde/fiyatta satış yapılmalı).", None
            
            model = None
            rapor = ""
            
            if df_gunluk['ortalama_fiyat'].nunique() < 2:
                rapor += "UYARI: Ürün hep aynı fiyattan satılmış. Talep modeli kurulamaz.\nOptimizasyon, mevcut ortalama satış adedine göre TAHMİNİDİR.\nDaha doğru bir analiz için ürünü farklı fiyatlardan satıp verileri tekrar yükleyin.\n\n"
                model = None # Modeli devredışı bırak
            else:
                X = df_gunluk[['ortalama_fiyat']]
                y = df_gunluk['toplam_adet']
                model = LinearRegression().fit(X, y)
                if model.coef_[0] >= 0:
                    rapor += "UYARI: Model, fiyat arttıkça satışların ARTTIĞINI söylüyor! Veri yetersiz veya hatalı.\n\n"
                    model = None

            # Fiyat aralığını belirle
            min_fiyat = max(maliyet * 1.1, df_gunluk['ortalama_fiyat'].min() * 0.8) 
            max_fiyat = df_gunluk['ortalama_fiyat'].max() * 1.5 
            
            test_prices = np.arange(min_fiyat, max_fiyat, fiyat_deneme_araligi)
            
            if test_prices.size == 0:
                return False, f"HATA: Geçerli bir fiyat aralığı bulunamadı. (Min: {min_fiyat}, Max: {max_fiyat})", None

            sonuclar = []
            for fiyat in test_prices:
                if model:
                    tahmini_adet = model.predict(np.array([[fiyat]]))[0]
                else:
                    tahmini_adet = df_gunluk['toplam_adet'].mean()
                
                tahmini_adet = max(0, tahmini_adet)
                tahmini_kar = (fiyat - maliyet) * tahmini_adet
                sonuclar.append({'test_fiyati': fiyat, 'tahmini_adet': tahmini_adet, 'tahmini_kar': tahmini_kar})

            if not sonuclar:
                return False, "HATA: Hiçbir sonuç hesaplanamadı.", None

            df_sonuclar = pd.DataFrame(sonuclar)
            
            optimum = df_sonuclar.loc[df_sonuclar['tahmini_kar'].idxmax()]
            
            mevcut_gunluk_satis = df_gunluk['toplam_adet'].mean()
            mevcut_kar = (mevcut_fiyat - maliyet) * mevcut_gunluk_satis
            
            rapor += (
                f"--- MEVCUT DURUM (Menü Fiyatı) ---\n"
                f"  Mevcut Fiyat: {mevcut_fiyat:.2f} TL\n"
                f"  Ort. Günlük Kar: {mevcut_kar:.2f} TL\n\n"
                f"--- OPTİMUM FİYAT TAVSİYESİ ---\n"
                f"  🏆 MAKSİMUM KAR İÇİN TAVSİYE EDİLEN FİYAT: {optimum['test_fiyati']:.2f} TL 🏆\n\n"
                f"  Bu fiyattan tahmini günlük satış: {optimum['tahmini_adet']:.1f} adet\n"
                f"  Tahmini maksimum günlük kar: {optimum['tahmini_kar']:.2f} TL"
            )
            
            chart_data = _generate_price_curve_data(model, maliyet, df_gunluk)
            return True, rapor, chart_data
            
        except Exception as e:
            return False, f"Optimizasyon hatası: {e}", None


# --- Motor 4 & 5 (Colab Hücre 10 & 11): Kategori ve Grup Analizi ---
def analiz_et_kategori_veya_grup(tip, isim, gun_sayisi=7):
    """
    Hem Kategori (Hücre 10) hem de Kategori Grubu (Hücre 11) analizini
    yapabilen birleşik fonksiyon.
    """
    try:
        if tip == 'kategori':
            df_satislar = _get_sales_by_filter('kategori', isim)
            grup_kolonu = 'isim' # Kategori içi ürünler
            baslik = f"KATEGORİ ANALİZİ: '{isim}'"
        elif tip == 'kategori_grubu':
            df_satislar = _get_sales_by_filter('kategori_grubu', isim)
            grup_kolonu = 'kategori' # Grup içi kategoriler
            baslik = f"KATEGORİ GRUBU ANALİZİ: '{isim}'"
        else:
            return False, "HATA: Geçersiz analiz tipi.", None

        if df_satislar is None or df_satislar.empty:
            return False, f"HATA: '{isim}' için hiç satış verisi bulunamadı.", None
        
        df_satislar['tarih'] = pd.to_datetime(df_satislar['tarih'])
        
        bugun = datetime.now().date()
        bu_periyot_basi = bugun - timedelta(days=gun_sayisi)
        onceki_periyot_basi = bu_periyot_basi - timedelta(days=gun_sayisi)

        df_bu_periyot = df_satislar[df_satislar['tarih'] >= pd.to_datetime(bu_periyot_basi)]
        df_onceki_periyot = df_satislar[
            (df_satislar['tarih'] >= pd.to_datetime(onceki_periyot_basi)) & 
            (df_satislar['tarih'] < pd.to_datetime(bu_periyot_basi))
        ]

        if df_bu_periyot.empty or df_onceki_periyot.empty:
            return False, f"UYARI: Karşılaştırma için yeterli veri bulunamadı. (Son {gun_sayisi} gün ve önceki {gun_sayisi} gün için ayrı ayrı veri gerekli).", None

        ozet_bu = _hesapla_kategori_ozeti(df_bu_periyot, grup_kolonu)
        ozet_onceki = _hesapla_kategori_ozeti(df_onceki_periyot, grup_kolonu)

        # Rapor için Metin Oluşturma
        rapor = f"{baslik}\n(Son {gun_sayisi} gün ile önceki {gun_sayisi} gün karşılaştırması)\n"
        rapor += "="*60 + "\n\n"

        rapor += f"--- ÖNCEKİ PERİYOT ({onceki_periyot_basi} - {bu_periyot_basi}) ---\n"
        rapor += f"  📊 TOPLAM KAR: {ozet_onceki['toplam_kari']:.2f} TL\n"
        rapor += "  Kar Payları (Grup içinde):\n"
        if not ozet_onceki['paylar']:
            rapor += "    - Veri yok.\n"
        for item_name, pay in ozet_onceki['paylar'].items():
            rapor += f"    - {item_name:<20}: %{pay:.1f}  ({ozet_onceki['karlar'].get(item_name, 0):.2f} TL)\n"
        
        rapor += f"\n--- BU PERİYOT (Son {gun_sayisi} Gün) ---\n"
        rapor += f"  📊 TOPLAM KAR: {ozet_bu['toplam_kari']:.2f} TL\n"
        rapor += "  Kar Payları (Grup içinde):\n"
        if not ozet_bu['paylar']:
            rapor += "    - Veri yok.\n"
        for item_name, pay in ozet_bu['paylar'].items():
            rapor += f"    - {item_name:<20}: %{pay:.1f}  ({ozet_bu['karlar'].get(item_name, 0):.2f} TL)\n"
        
        rapor += "\n" + "="*60 + "\n"
        rapor += "  STRATEJİST TAVSİYESİ:\n"
        
        fark = ozet_bu['toplam_kari'] - ozet_onceki['toplam_kari']
        if fark > 0:
            rapor += f"  ✅ BAŞARILI! '{isim}' grubunun/kategorisinin toplam karı {fark:.2f} TL ARTTI."
        else:
            rapor += f"  ❌ DİKKAT! '{isim}' grubunun/kategorisinin toplam karı {abs(fark):.2f} TL AZALDI.\n"
            if tip == 'kategori_grubu':
                rapor += "  Bu durum 'çapraz yamyamlık' (cannibalization) etkisi olabilir. Detayları inceleyin.\n"
            else:
                rapor += "  Bu durum 'iç yamyamlık' (cannibalization) etkisi olabilir.\n"
            rapor += "  Bu fiyat politikasını GÖZDEN GEÇİRİN."
        
        # Chart.js için Veri Hazırlama
        labels = sorted(list(set(ozet_onceki['karlar'].keys()) | set(ozet_bu['karlar'].keys())))
        data_onceki = [ozet_onceki['karlar'].get(label, 0) for label in labels]
        data_bu = [ozet_bu['karlar'].get(label, 0) for label in labels]
        
        chart_data = {
            'type': 'bar', # Grafik tipi
            'labels': labels,
            'datasets': [
                {
                    'label': f'Önceki {gun_sayisi} Gün Kâr (TL)',
                    'data': data_onceki,
                    'backgroundColor': 'rgba(255, 99, 132, 0.5)',
                    'borderColor': 'rgb(255, 99, 132)',
                    'borderWidth': 1
                },
                {
                    'label': f'Son {gun_sayisi} Gün Kâr (TL)',
                    'data': data_bu,
                    'backgroundColor': 'rgba(54, 162, 235, 0.5)',
                    'borderColor': 'rgb(54, 162, 235)',
                    'borderWidth': 1
                }
            ]
        }
        
        return True, rapor, json.dumps(chart_data)

    except Exception as e:
        print(f"Stratejik analiz hatası: {e}")
        return False, f"Stratejik analiz hatası: {e}", None
