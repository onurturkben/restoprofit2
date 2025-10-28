import os
import json
import pandas as pd
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from database import (
    db, init_db, Hammadde, Urun, Recete, SatisKaydi, User,
    guncelle_tum_urun_maliyetleri
)
from sqlalchemy.exc import IntegrityError
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from sqlalchemy import func
from werkzeug.utils import secure_filename
import warnings
from sklearn.linear_model import LinearRegression

# --- Analiz Motorlarını "Beyinden" İçe Aktar ---
from analysis_engine import (
    hesapla_hedef_marj,
    simule_et_fiyat_degisikligi,
    bul_optimum_fiyat,
    analiz_et_kategori_veya_grup
)

# --- UYGULAMA KURULUMU ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'renderda_bunu_kesin_degistirmelisiniz123')
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2 MB limit

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

init_db(app) # Veritabanını başlat
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Bu sayfayı görüntülemek için lütfen giriş yapın."
login_manager.login_message_category = "warning"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --- İLK KULLANICIYI OLUŞTUR ---
with app.app_context():
    db.create_all()
    if not User.query.first():
        print("İlk admin kullanıcısı oluşturuluyor...")
        # Lütfen bu şifreyi ilk girişten sonra hemen değiştirin!
        hashed_password = bcrypt.generate_password_hash("RestoranSifrem!2025").decode('utf-8')
        admin_user = User(username="onur", password_hash=hashed_password)
        db.session.add(admin_user)
        try:
            db.session.commit()
            print("Güvenli kullanıcı 'onur' oluşturuldu.")
        except Exception as e:
            db.session.rollback()
            print(f"Kullanıcı oluşturulurken hata: {e}")

# --- CONTEXT PROCESSOR ---
@app.context_processor
def inject_settings():
    # Bu, tüm templatelerde 'current_user' değişkenini kullanılabilir yapar
    # 'settings'i Ayarlar tablosu kaldırıldığı için sildik.
    return dict(current_user=current_user, site_name="RestoProfit")

# --- GÜVENLİK SAYFALARI ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard')) 
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Kullanıcı adı veya şifre hatalı.', 'danger')
    return render_template('login.html', title='Giriş Yap')

@app.route('/logout')
@login_required 
def logout():
    logout_user()
    flash('Başarıyla çıkış yaptınız.', 'info')
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not bcrypt.check_password_hash(current_user.password_hash, current_password):
            flash('Mevcut şifreniz hatalı.', 'danger')
            return redirect(url_for('change_password'))
            
        if new_password != confirm_password:
            flash('Yeni şifreler birbiriyle eşleşmiyor.', 'danger')
            return redirect(url_for('change_password'))
            
        if len(new_password) < 6:
            flash('Yeni şifreniz en az 6 karakter olmalıdır.', 'danger')
            return redirect(url_for('change_password'))

        try:
            hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
            current_user.password_hash = hashed_password
            db.session.commit()
            flash('Şifreniz başarıyla güncellendi. Lütfen yeni şifrenizle tekrar giriş yapın.', 'success')
            return redirect(url_for('logout')) 
            
        except Exception as e:
            db.session.rollback()
            flash(f"Şifre güncellenirken bir hata oluştu: {e}", 'danger')
            return redirect(url_for('change_password'))
            
    return render_template('change_password.html', title='Şifre Değiştir')


# --- ANA SAYFA ---
@app.route('/')
@login_required 
def dashboard():
    try:
        toplam_satis_kaydi = db.session.query(SatisKaydi).count()
        toplam_urun = db.session.query(Urun).count()
        summary = {
            'toplam_satis_kaydi': toplam_satis_kaydi,
            'toplam_urun': toplam_urun
        }
    except Exception as e:
        summary = {'toplam_satis_kaydi': 0, 'toplam_urun': 0}
        flash(f'Veritabanı bağlantı hatası: {e}', 'danger')

    return render_template('dashboard.html', title='Ana Ekran', summary=summary)


# --- VERİ YÖNETİMİ ---
@app.route('/upload-excel', methods=['POST'])
@login_required
def upload_excel():
    if 'excel_file' not in request.files:
        flash('Dosya kısmı bulunamadı', 'danger')
        return redirect(url_for('dashboard'))
    
    file = request.files['excel_file']
    if file.filename == '':
        flash('Dosya seçilmedi', 'danger')
        return redirect(url_for('dashboard'))
    
    if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
        try:
            df = pd.read_excel(file)
            required_columns = ['Urun_Adi', 'Adet', 'Toplam_Tutar', 'Tarih']
            missing_columns = [col for col in required_columns if not col in df.columns]
            if missing_columns:
                raise ValueError(f"Excel dosyanızda şu kolonlar eksik: {', '.join(missing_columns)}")
            
            urunler_db = Urun.query.all()
            urun_eslestirme_haritasi = {u.excel_adi: u.id for u in urunler_db}
            
            with app.app_context():
                urun_maliyet_haritasi = {u.id: u.hesaplanan_maliyet for u in urunler_db}
                yeni_kayit_listesi = []
                taninmayan_urunler = set()
                hatali_satirlar = []
                
                for index, satir in df.iterrows():
                    try:
                        excel_urun_adi = str(satir['Urun_Adi']).strip()
                        adet = int(satir['Adet'])
                        toplam_tutar = float(satir['Toplam_Tutar'])
                        tarih = pd.to_datetime(satir['Tarih'])
                        
                        urun_id = urun_eslestirme_haritasi.get(excel_urun_adi)
                        if not urun_id:
                            taninmayan_urunler.add(excel_urun_adi)
                            continue
                        if adet <= 0: continue
                        
                        o_anki_maliyet = urun_maliyet_haritasi.get(urun_id, 0.0)
                        if o_anki_maliyet is None: o_anki_maliyet = 0.0

                        hesaplanan_toplam_maliyet = o_anki_maliyet * adet
                        hesaplanan_kar = toplam_tutar - hesaplanan_toplam_maliyet
                        hesaplanan_birim_fiyat = toplam_tutar / adet if adet != 0 else 0
                        
                        yeni_kayit = SatisKaydi(
                            urun_id=urun_id, tarih=tarih, adet=adet, toplam_tutar=toplam_tutar,
                            hesaplanan_birim_fiyat=hesaplanan_birim_fiyat,
                            hesaplanan_maliyet=hesaplanan_toplam_maliyet,
                            hesaplanan_kar=hesaplanan_kar
                        )
                        yeni_kayit_listesi.append(yeni_kayit)
                    except Exception as row_error:
                        print(f"Satır {index + 2} işlenirken hata: {row_error}")
                        hatali_satirlar.append(index + 2)
                        continue
                
                if yeni_kayit_listesi:
                    db.session.add_all(yeni_kayit_listesi)
                    db.session.commit()
                    flash(f'Başarılı! {len(yeni_kayit_listesi)} adet satış kaydı veritabanına işlendi.', 'success')
                else:
                     flash('Excel dosyasından işlenecek geçerli satış kaydı bulunamadı.', 'warning')

                if taninmayan_urunler:
                    flash(f"UYARI: Şu ürünler 'Menü Yönetimi'nde bulunamadı ve atlandı: {', '.join(taninmayan_urunler)}", 'warning')
                if hatali_satirlar:
                     flash(f"UYARI: Excel'deki şu satırlar hatalı veri içerdiği için atlandı: {', '.join(map(str, sorted(list(set(hatali_satirlar)))))}", 'warning')

        except ValueError as ve:
            flash(f"HATA OLUŞTU: {ve}", 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f"BEKLENMEDİK HATA: {e}. Lütfen Excel formatınızı kontrol edin.", 'danger')
        
    return redirect(url_for('dashboard'))

# --- YÖNETİM PANELİ (CRUD) ---

@app.route('/admin')
@login_required
def admin_panel():
    try:
        hammaddeler = Hammadde.query.order_by(Hammadde.isim).all()
        urunler = Urun.query.order_by(Urun.isim).all()
        # Düzeltilmiş sorgu (join(Hammadde) eklendi):
        receteler = Recete.query.join(Urun, Urun.id == Recete.urun_id).join(Hammadde, Hammadde.id == Recete.hammadde_id).order_by(Urun.isim, Hammadde.isim).all()
    except Exception as e:
        flash(f'Veritabanı hatası: {e}', 'danger')
        hammaddeler, urunler, receteler = [], [], []
            
    return render_template('admin.html', title='Menü Yönetimi', 
                           hammaddeler=hammaddeler, 
                           urunler=urunler, 
                           receteler=receteler)

# --- HAMMADDE CRUD ---
@app.route('/add-material', methods=['POST'])
@login_required
def add_material():
    try:
        isim = request.form.get('h_isim').strip()
        birim = request.form.get('h_birim').strip()
        fiyat_str = request.form.get('h_fiyat')
        
        if not isim or not birim or not fiyat_str:
            flash("HATA: Tüm hammadde alanları doldurulmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))
            
        fiyat = float(fiyat_str.replace(',', '.')) 

        if fiyat < 0: # 0 olabilir (örn: su)
            flash("HATA: Hammadde fiyatı negatif olamaz.", 'danger')
            return redirect(url_for('admin_panel'))
            
        yeni_hammadde = Hammadde(isim=isim, maliyet_birimi=birim, maliyet_fiyati=fiyat)
        db.session.add(yeni_hammadde)
        db.session.commit()
        flash(f"Başarılı! '{isim}' hammaddesi eklendi.", 'success')
    
    except IntegrityError: 
        db.session.rollback()
        flash(f"HATA: '{isim}' adında bir hammadde zaten mevcut.", 'danger')
    except ValueError:
        db.session.rollback()
        flash("HATA: Fiyat geçerli bir sayı olmalıdır.", 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Hammadde eklenirken bir hata oluştu: {e}", 'danger')
        
    return redirect(url_for('admin_panel'))

@app.route('/edit-material/<int:id>', methods=['POST'])
@login_required
def edit_material(id):
    try:
        hammadde = db.session.get(Hammadde, id)
        if not hammadde:
            flash('Hammadde bulunamadı.', 'danger')
            return redirect(url_for('admin_panel'))
        
        isim = request.form.get('isim').strip()
        birim = request.form.get('birim').strip()
        fiyat_str = request.form.get('fiyat')

        if not isim or not birim or not fiyat_str:
            flash("HATA: Tüm hammadde alanları doldurulmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))

        fiyat = float(fiyat_str.replace(',', '.'))

        if fiyat < 0:
            flash("HATA: Hammadde fiyatı negatif olamaz.", 'danger')
            return redirect(url_for('admin_panel'))
        
        if hammadde.isim != isim and Hammadde.query.filter(Hammadde.isim == isim, Hammadde.id != id).first():
             flash(f"HATA: '{isim}' adında başka bir hammadde zaten mevcut.", 'danger')
             return redirect(url_for('admin_panel'))

        hammadde.isim = isim
        hammadde.maliyet_birimi = birim
        hammadde.maliyet_fiyati = fiyat
        db.session.commit()
        guncelle_tum_urun_maliyetleri() # Fiyat değiştiği için maliyetleri yeniden hesapla
        flash(f"'{hammadde.isim}' güncellendi.", 'success')

    except ValueError:
        db.session.rollback()
        flash("HATA: Fiyat geçerli bir sayı olmalıdır.", 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Güncelleme sırasında bir hata oluştu: {e}", 'danger')
    return redirect(url_for('admin_panel'))

@app.route('/delete-material/<int:id>', methods=['POST'])
@login_required
def delete_material(id):
    try:
        hammadde = db.session.get(Hammadde, id)
        if hammadde:
            if hammadde.receteler.first():
                flash(f"HATA: '{hammadde.isim}' bir veya daha fazla reçetede kullanıldığı için silinemez. Önce ilgili reçeteleri silin.", 'danger')
                return redirect(url_for('admin_panel'))
                
            db.session.delete(hammadde)
            db.session.commit()
            flash(f"'{hammadde.isim}' silindi.", 'success')
        else:
            flash("Hammadde bulunamadı.", 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: {e}", 'danger')
    return redirect(url_for('admin_panel'))

# --- ÜRÜN CRUD ---
@app.route('/add-product', methods=['POST'])
@login_required
def add_product():
    try:
        isim = request.form.get('u_isim').strip()
        excel_adi = request.form.get('u_excel_adi').strip()
        fiyat_str = request.form.get('u_fiyat')
        kategori = request.form.get('u_kategori').strip()
        grup = request.form.get('u_grup').strip()

        if not isim or not excel_adi or not fiyat_str or not kategori or not grup:
            flash("HATA: Tüm ürün alanları doldurulmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))

        fiyat = float(fiyat_str.replace(',', '.'))
        
        if fiyat <= 0:
            flash("HATA: Ürün fiyatı pozitif olmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))

        yeni_urun = Urun(
            isim=isim, 
            excel_adi=excel_adi, 
            mevcut_satis_fiyati=fiyat, 
            kategori=kategori, 
            kategori_grubu=grup,
            hesaplanan_maliyet=0
        )
        db.session.add(yeni_urun)
        db.session.commit()
        flash(f"Başarılı! '{isim}' ürünü eklendi. Şimdi reçetesini oluşturun.", 'success')
    
    except IntegrityError:
        db.session.rollback()
        flash(f"HATA: '{isim}' adında veya '{excel_adi}' Excel adına sahip bir ürün zaten mevcut.", 'danger')
    except ValueError:
        db.session.rollback()
        flash("HATA: Fiyat geçerli bir sayı olmalıdır.", 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Ürün eklenirken bir hata oluştu: {e}", 'danger')
        
    return redirect(url_for('admin_panel'))
    
@app.route('/edit-product/<int:id>', methods=['POST'])
@login_required
def edit_product(id):
    try:
        urun = db.session.get(Urun, id)
        if not urun:
            flash('Ürün bulunamadı.', 'danger')
            return redirect(url_for('admin_panel'))
            
        isim = request.form.get('isim').strip()
        excel_adi = request.form.get('excel_adi').strip()
        fiyat_str = request.form.get('fiyat')
        kategori = request.form.get('kategori').strip()
        grup = request.form.get('grup').strip()

        if not isim or not excel_adi or not fiyat_str or not kategori or not grup:
            flash("HATA: Tüm ürün alanları doldurulmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))

        fiyat = float(fiyat_str.replace(',', '.'))

        if fiyat <= 0:
            flash("HATA: Ürün fiyatı pozitif olmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))
            
        if urun.isim != isim and Urun.query.filter(Urun.isim == isim, Urun.id != id).first():
             flash(f"HATA: '{isim}' adında başka bir ürün zaten mevcut.", 'danger')
             return redirect(url_for('admin_panel'))
        if urun.excel_adi != excel_adi and Urun.query.filter(Urun.excel_adi == excel_adi, Urun.id != id).first():
             flash(f"HATA: '{excel_adi}' Excel adına sahip başka bir ürün zaten mevcut.", 'danger')
             return redirect(url_for('admin_panel'))

        urun.isim = isim
        urun.excel_adi = excel_adi
        urun.mevcut_satis_fiyati = fiyat
        urun.kategori = kategori
        urun.kategori_grubu = grup
        db.session.commit()
        
        guncelle_tum_urun_maliyetleri()
        
        flash(f"'{urun.isim}' güncellendi.", 'success')

    except ValueError:
        db.session.rollback()
        flash("HATA: Fiyat geçerli bir sayı olmalıdır.", 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Ürün güncellenirken bir hata oluştu: {e}", 'danger')
    return redirect(url_for('admin_panel'))

@app.route('/delete-product/<int:id>', methods=['POST'])
@login_required
def delete_product(id):
    try:
        urun = db.session.get(Urun, id)
        if urun:
            db.session.delete(urun)
            db.session.commit()
            flash(f"'{urun.isim}' ürünü ve ilgili tüm kayıtlar silindi.", 'success')
        else:
            flash("Ürün bulunamadı.", 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Ürün silinirken bir hata oluştu: {e}", 'danger')
    return redirect(url_for('admin_panel'))

# --- REÇETE CRUD ---
@app.route('/add-recipe', methods=['POST'])
@login_required
def add_recipe():
    try:
        urun_id_str = request.form.get('r_urun_id')
        hammadde_id_str = request.form.get('r_hammadde_id')
        miktar_str = request.form.get('r_miktar')

        if not urun_id_str or not hammadde_id_str or not miktar_str:
            flash("HATA: Reçete için ürün, hammadde ve miktar seçilmelidir.", 'danger')
            return redirect(url_for('admin_panel'))
            
        urun_id = int(urun_id_str)
        hammadde_id = int(hammadde_id_str)
        miktar = float(miktar_str.replace(',', '.'))

        if miktar <= 0:
            flash("HATA: Miktar pozitif olmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))
            
        existing_recipe = Recete.query.filter_by(urun_id=urun_id, hammadde_id=hammadde_id).first()
        if existing_recipe:
            flash("UYARI: Bu ürün için bu hammadde zaten reçetede vardı. Miktarı güncellendi.", 'warning')
            existing_recipe.miktar = miktar
        else:
            yeni_recete = Recete(urun_id=urun_id, hammadde_id=hammadde_id, miktar=miktar)
            db.session.add(yeni_recete)
            flash("Başarılı! Reçete kalemi eklendi.", 'success')
        
        db.session.commit()
        guncelle_tum_urun_maliyetleri() # Maliyeti hemen güncelle
        
    except ValueError:
        db.session.rollback()
        flash("HATA: Miktar geçerli bir sayı olmalıdır.", 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Reçete işlenirken bir hata oluştu: {e}", 'danger')
        
    return redirect(url_for('admin_panel'))

@app.route('/edit-recipe/<int:id>', methods=['POST'])
@login_required
def edit_recipe(id):
    try:
        recete_item = db.session.get(Recete, id)
        if not recete_item:
            flash('Reçete kalemi bulunamadı.', 'danger')
            return redirect(url_for('admin_panel'))

        miktar_str = request.form.get('edit_r_miktar')
        if not miktar_str:
            flash("HATA: Miktar alanı boş olamaz.", 'danger')
            return redirect(url_for('admin_panel'))

        miktar = float(miktar_str.replace(',', '.'))
        
        if miktar <= 0:
            flash("HATA: Miktar pozitif olmalıdır.", 'danger')
            return redirect(url_for('admin_panel'))

        recete_item.miktar = miktar
        db.session.commit()
        guncelle_tum_urun_maliyetleri()
        flash(f"'{recete_item.urun.isim}' ürününün '{recete_item.hammadde.isim}' reçete kalemi güncellendi.", 'success')
        
    except ValueError:
        db.session.rollback()
        flash("HATA: Miktar geçerli bir sayı olmalıdır.", 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Reçete güncellenirken bir hata oluştu: {e}", 'danger')
    return redirect(url_for('admin_panel'))


@app.route('/delete-recipe/<int:id>', methods=['POST'])
@login_required
def delete_recipe(id):
    try:
        recete_item = db.session.get(Recete, id)
        if recete_item:
            urun_adi = recete_item.urun.isim # Silmeden önce ismi alalım
            hammadde_adi = recete_item.hammadde.isim
            db.session.delete(recete_item)
            db.session.commit()
            guncelle_tum_urun_maliyetleri() # Maliyeti yeniden güncelle
            flash(f"'{urun_adi}' ürününden '{hammadde_adi}' kalemi silindi.", 'success')
        else:
            flash("Reçete kalemi bulunamadı.", 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Reçete kalemi silinirken bir hata oluştu: {e}", 'danger')
    return redirect(url_for('admin_panel'))


# --- VERİ YÖNETİMİ ---
@app.route('/delete-sales-by-date', methods=['POST'])
@login_required
def delete_sales_by_date():
    try:
        date_str = request.form.get('delete_date')
        if not date_str:
            flash("HATA: Lütfen silmek için geçerli bir tarih seçin.", 'danger')
            return redirect(url_for('admin_panel'))
            
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        
        num_deleted = db.session.query(SatisKaydi).filter(
            func.date(SatisKaydi.tarih) == target_date
        ).delete(synchronize_session=False) # Performansı artırmak için
        db.session.commit()
        
        if num_deleted > 0:
            flash(f"Başarılı! {target_date.strftime('%d %B %Y')} tarihine ait {num_deleted} adet satış kaydı kalıcı olarak silindi.", 'success')
        else:
            flash(f"Bilgi: {target_date.strftime('%d %B %Y')} tarihinde zaten hiç satış kaydı bulunamadı.", 'info')
            
    except ValueError:
         flash("HATA: Geçersiz tarih formatı.", 'danger')
         db.session.rollback()
    except Exception as e:
        db.session.rollback()
        flash(f"HATA: Satış kayıtları silinirken bir hata oluştu: {e}", 'danger')
        
    return redirect(url_for('admin_panel'))


# --- ŞİFRE DEĞİŞTİRME ---
@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not bcrypt.check_password_hash(current_user.password_hash, current_password):
            flash('Mevcut şifreniz hatalı.', 'danger')
            return redirect(url_for('change_password'))
            
        if new_password != confirm_password:
            flash('Yeni şifreler birbiriyle eşleşmiyor.', 'danger')
            return redirect(url_for('change_password'))
            
        if len(new_password) < 6:
            flash('Yeni şifreniz en az 6 karakter olmalıdır.', 'danger')
            return redirect(url_for('change_password'))

        try:
            hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
            current_user.password_hash = hashed_password
            db.session.commit()
            flash('Şifreniz başarıyla güncellendi. Lütfen yeni şifrenizle tekrar giriş yapın.', 'success')
            return redirect(url_for('logout')) 
            
        except Exception as e:
            db.session.rollback()
            flash(f"Şifre güncellenirken bir hata oluştu: {e}", 'danger')
            return redirect(url_for('change_password'))
            
    return render_template('change_password.html', title='Şifre Değiştir')


# --- ANALİZ RAPORLARI SAYFASI ---
@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    try:
        urunler_db = Urun.query.order_by(Urun.isim).all()
        urun_listesi = [u.isim for u in urunler_db]
        
        kategoriler_db = db.session.query(Urun.kategori).distinct().order_by(Urun.kategori).all()
        kategori_listesi = sorted([k[0] for k in kategoriler_db if k[0]])
        
        gruplar_db = db.session.query(Urun.kategori_grubu).distinct().order_by(Urun.kategori_grubu).all()
        grup_listesi = sorted([g[0] for g in gruplar_db if g[0]])
        
    except Exception as e:
        flash(f'Veritabanından listeler çekilirken hata oluştu: {e}', 'danger')
        urun_listesi, kategori_listesi, grup_listesi = [], [], []

    analiz_sonucu = None
    chart_data = None 
    analiz_tipi_baslik = "" 
    
    if request.method == 'POST':
        try:
            analiz_tipi = request.form.get('analiz_tipi')
            
            # Formdan gelen değerleri al
            urun_ismi = request.form.get('urun_ismi')
            kategori_ismi = request.form.get('kategori_ismi')
            grup_ismi = request.form.get('grup_ismi')
            gun_sayisi_str = request.form.get('gun_sayisi', '7')
            
            try:
                gun_sayisi = int(gun_sayisi_str) if gun_sayisi_str.isdigit() else 7
            except ValueError:
                gun_sayisi = 7
                flash("Geçersiz gün sayısı, varsayılan 7 gün kullanıldı.", "warning")

            
            if analiz_tipi == 'hedef_marj':
                if not urun_ismi: raise ValueError("Lütfen bir ürün seçin.")
                hedef_marj_str = request.form.get('hedef_marj')
                if not hedef_marj_str: raise ValueError("Lütfen bir hedef marj girin.")
                
                analiz_tipi_baslik = f"Hedef Marj: {urun_ismi}"
                hedef_marj = float(hedef_marj_str.replace(',', '.'))
                success, sonuc, chart_data_json = hesapla_hedef_marj(urun_ismi, hedef_marj)
                analiz_sonucu = sonuc
                chart_data = chart_data_json
            
            elif analiz_tipi == 'simulasyon':
                if not urun_ismi: raise ValueError("Lütfen bir ürün seçin.")
                yeni_fiyat_str = request.form.get('yeni_fiyat')
                if not yeni_fiyat_str: raise ValueError("Lütfen bir fiyat girin.")

                yeni_fiyat = float(yeni_fiyat_str.replace(',', '.'))
                analiz_tipi_baslik = f"Fiyat Simülasyonu: {urun_ismi}"
                success, sonuc, chart_data_json = simule_et_fiyat_degisikligi(urun_ismi, yeni_fiyat)
                analiz_sonucu = sonuc
                chart_data = chart_data_json
                
            elif analiz_tipi == 'optimum_fiyat':
                if not urun_ismi: raise ValueError("Lütfen bir ürün seçin.")

                analiz_tipi_baslik = f"Optimum Fiyat: {urun_ismi}"
                success, sonuc, chart_data_json = bul_optimum_fiyat(urun_ismi)
                analiz_sonucu = sonuc
                chart_data = chart_data_json 
                
            elif analiz_tipi == 'kategori':
                if not kategori_ismi: raise ValueError("Lütfen bir kategori seçin.")
                
                analiz_tipi_baslik = f"Kategori Analizi: {kategori_ismi} ({gun_sayisi} gün)"
                success, sonuc, chart_data_json = analiz_et_kategori_veya_grup('kategori', kategori_ismi, gun_sayisi)
                analiz_sonucu = sonuc
                chart_data = chart_data_json
                
            elif analiz_tipi == 'grup':
                if not grup_ismi: raise ValueError("Lütfen bir grup seçin.")
                
                analiz_tipi_baslik = f"Grup Analizi: {grup_ismi} ({gun_sayisi} gün)"
                success, sonuc, chart_data_json = analiz_et_kategori_veya_grup('kategori_grubu', grup_ismi, gun_sayisi)
                analiz_sonucu = sonuc
                chart_data = chart_data_json
            
            else:
                success, sonuc = False, "Geçersiz analiz tipi."
                flash(sonuc, 'warning')

            if not success:
                flash(sonuc, 'danger')
                chart_data = None # Hata varsa grafik gönderme

        except ValueError as ve:
             flash(f"Giriş hatası: {ve}", 'danger')
             analiz_sonucu = None
             chart_data = None
        except Exception as e:
            db.session.rollback()
            flash(f"Analiz sırasında beklenmedik bir hata oluştu: {e}", 'danger')
            analiz_sonucu = None
            chart_data = None

    return render_template('reports.html', title='Analiz Motorları',
                           urun_listesi=urun_listesi,
                           kategori_listesi=kategori_listesi,
                           grup_listesi=grup_listesi,
                           analiz_sonucu=analiz_sonucu,
                           chart_data=chart_data,
                           analiz_tipi_baslik=analiz_tipi_baslik)

# Render.com'un uygulamayı çalıştırması için
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    # app.run(debug=True) # Lokal testler için
    app.run(host='0.0.0.0', port=port) # Render için
