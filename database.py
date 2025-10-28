import os
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from flask_login import UserMixin
from sqlalchemy.orm import relationship, backref
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Date

# Veritabanı nesnesini başlat
db = SQLAlchemy()

# 1. Kullanıcı Modeli (Giriş ve Yetkilendirme için)
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

    def __repr__(self):
        return f'<User {self.username}>'

# 2. Hammadde Modeli
class Hammadde(db.Model):
    __tablename__ = 'hammaddeler'
    id = db.Column(db.Integer, primary_key=True)
    isim = db.Column(db.String(100), unique=True, nullable=False)
    maliyet_birimi = db.Column(db.String(20), nullable=False, default='gr') # Örn: kg, litre, adet
    maliyet_fiyati = db.Column(db.Float, nullable=False)
    guncellenme_tarihi = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Bir hammaddenin birden fazla reçete kaleminde olabileceği ilişki
    receteler = db.relationship('Recete', back_populates='hammadde', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Hammadde {self.isim}>'

# 3. Ürün Modeli (Menüdeki Kalemler)
class Urun(db.Model):
    __tablename__ = 'urunler'
    id = db.Column(db.Integer, primary_key=True)
    isim = db.Column(db.String(100), unique=True, nullable=False)
    excel_adi = db.Column(db.String(100), unique=True, nullable=False) # Excel/Adisyon'daki adı
    mevcut_satis_fiyati = db.Column(db.Float, nullable=False)
    hesaplanan_maliyet = db.Column(db.Float, default=0.0) # Reçeteden anlık hesaplanır
    kategori = db.Column(db.String(100)) # Örn: Burgerler, İçecekler
    kategori_grubu = db.Column(db.String(100)) # Örn: Ana Yemekler, Alkolsüz İçecekler
    
    # İlişkiler: Bir ürünün birden fazla reçete kalemi ve satış kaydı olabilir
    receteler = db.relationship('Recete', back_populates='urun', cascade="all, delete-orphan")
    satislar = db.relationship('SatisKaydi', back_populates='urun', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Urun {self.isim}>'

# 4. Reçete Modeli (Ara Tablo: Ürünler ve Hammaddeler)
class Recete(db.Model):
    __tablename__ = 'receteler'
    id = db.Column(db.Integer, primary_key=True)
    miktar = db.Column(db.Float, nullable=False)
    
    # Foreign Keys
    urun_id = db.Column(db.Integer, db.ForeignKey('urunler.id'), nullable=False)
    hammadde_id = db.Column(db.Integer, db.ForeignKey('hammaddeler.id'), nullable=False)
    
    # Relationships
    urun = db.relationship('Urun', back_populates='receteler')
    hammadde = db.relationship('Hammadde', back_populates='receteler')

    def __repr__(self):
        return f'<Recete: {self.urun.isim} - {self.hammadde.isim}>'

# 5. Satış Kaydı Modeli (Excel'den Gelen Veri)
class SatisKaydi(db.Model):
    __tablename__ = 'satis_kayitlari'
    id = db.Column(db.Integer, primary_key=True)
    urun_id = db.Column(db.Integer, db.ForeignKey('urunler.id'), nullable=False)
    adet = db.Column(db.Integer, nullable=False)
    toplam_tutar = db.Column(db.Float, nullable=False)
    hesaplanan_birim_fiyat = db.Column(db.Float)
    hesaplanan_maliyet = db.Column(db.Float) # Satış anındaki maliyet (Teknik borcu çözer)
    hesaplanan_kar = db.Column(db.Float)
    tarih = db.Column(db.DateTime, nullable=False)
    
    urun = db.relationship('Urun', back_populates='satislar')

# --- YARDIMCI FONKSİYONLAR ---

def init_db(app):
    """ Veritabanını Flask uygulamasına bağlar ve tabloları oluşturur. """
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith("postgres://"):
        # Render PostgreSQL URL'sini SQLAlchemy uyumlu hale getir
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    else:
        # Lokal geliştirme için SQLite fallback
        instance_path = os.path.join(app.instance_path)
        if not os.path.exists(instance_path):
            os.makedirs(instance_path)
        database_url = f'sqlite:///{os.path.join(instance_path, "app.db")}'
        print(f"UYARI: DATABASE_URL bulunamadı, lokal SQLite kullanılıyor: {database_url}")

    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            
    db.init_app(app)
    
    with app.app_context():
        print("Veritabanı tabloları kontrol ediliyor/oluşturuluyor...")
        db.create_all()
        print("Veritabanı yapısı hazır.")

def guncelle_tum_urun_maliyetleri():
    """ 
    Tüm ürünlerin maliyetlerini reçetelere göre günceller.
    Hammadde fiyatı değiştiğinde veya reçete güncellendiğinde çalıştırılır.
    """
    try:
        urunler = Urun.query.all()
        for urun in urunler:
            toplam_maliyet = 0.0
            receteler = Recete.query.filter_by(urun_id=urun.id).all()
            for recete_kalemi in receteler:
                if recete_kalemi.hammadde:
                    toplam_maliyet += recete_kalemi.miktar * recete_kalemi.hammadde.maliyet_fiyati
            urun.hesaplanan_maliyet = round(toplam_maliyet, 2)
        db.session.commit()
        return True, "Tüm ürün maliyetleri başarıyla güncellendi."
    except Exception as e:
        db.session.rollback()
        print(f"Maliyet güncelleme hatası: {e}")
        return False, f"Maliyet güncelleme hatası: {e}"
