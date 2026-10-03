# adops-guard (Türkçe özet)

Google Ads API ve Meta Marketing API için komut satırı aracı. Küçük ekipler reklam işlerini kodla
yürütsün, ama yanlışlıkla para harcanmasın diye yazıldı.

Ayrıntılı belge İngilizce: [README.md](README.md). İlk kurulum için aşağıdaki
[Kurulum](#kurulum) bölümüne bakın.

## Nasıl çalışır?

- Her yazma işlemi, `--apply` verilmedikçe **deneme** olarak çalışır. Araç önce planı gösterir
  (eski değer -> yeni değer). Google'da isteği platformun kendi denetiminden de geçirir; Meta'da bu
  denetimi `--validate` ile siz istersiniz. Denemede hiçbir şey değişmez.
- `--apply` ile önce aynı denetim yapılır, sonra değişiklik gönderilir; nesne API'den **geri
  okunur** ve gönderilenle karşılaştırılır. Her adım bir günlüğe (`journal.jsonl`) yazılır.
- Bütçeyi artıran, harcama tavanını yükselten ya da kaldıran, bir şeyi yayına alan her işlem ayar
  dosyasındaki **üst sınırla** karşılaştırılır. Sınır yoksa ya da aşılıyorsa işlem reddedilir.
  Harcamayı azaltan işlemler hiç engellenmez.
- `--override-limit` ile sınır tek bir komut için aşılabilir; plan bunu açıkça yazar.
- Anahtarlar ve token'lar yalnız ortam değişkenlerinden, yalnız sizin okuyabildiğiniz (chmod 600)
  dosyalardan, macOS Anahtar Zinciri'nden ya da AWS SSM'den okunur. Komut satırına, URL'lere,
  günlüğe ya da durum dosyasına asla yazılmaz; hata mesajlarında maskelenir.
- Python 3.10 ve üstü yeter; ek kütüphane gerekmez.

## Neler var?

- **Google Ads:** durum, rapor, bütçe değiştirme, durdurma/yayına alma, Demand Gen kanal ayarları
  (YouTube in-stream, akış, Shorts, Discover...), tıklama kalitesi denetimi, harcama bekçisi
  (`spend-watch`), web sitesi dönüşüm işlemi oluşturma.
- **Meta:** durum, rapor, yayın durumu ve bütçe değiştirme (geri okumalı), kampanya harcama tavanı,
  kullanım sınırı koruması (%85'te durur), videoların Instagram'a uygunluk denetimi.
- **Kurulum denetimi:** `adops-guard doctor` ayar dosyasını, kimlik bilgilerini, OAuth'u, hesap
  erişimini, hesap bütçesini ve harcama limitini, Meta izinlerini, reklam hesabının durumunu ve
  Instagram kimliğini yalnız okuyarak denetler; her sorunun altına çözümünü yazar.
- **Açılış sayfası parçası:** Google Consent Mode v2 (AEA, Birleşik Krallık ve İsviçre'de ziyaretçi
  onay verene kadar kapalı), App Store düğmesine tıklamada dönüşüm, `?ct=` ile App Store kampanya
  bağlantıları.

Harcama bekçisi, harcamayı hesap bütçesinin (aylık fatura) başlangıcından sayar. Hesap bütçesi
olmayan hesaplarda `--since YYYY-AA-GG` ister; tüm zamanların harcamasını saymaz.

## Kurulum

Her tıklamanın tek tek anlatıldığı rehber İngilizce: [docs/setup.md](docs/setup.md) (platform başına
yaklaşık 15 dakika). Konsol menülerinin adı sık değişir; rehber yolu ve olası etiketleri birlikte
verir. Özetle:

**Ayar dosyası**

Örnek ayar dosyasını `adops-guard.ini` adıyla kopyalayın (komutlar aşağıda, "Hızlı başlangıç"ta).
Hesap kimlikleri ve token kaynağı örnekte `#` ile kapalıdır; adımlar ilerledikçe bu satırların
başındaki `#` işaretini kaldırıp kendi değerlerinizi yazın. Dosyaya ikinci bir `[google]` ya da
`[meta]` bloğu yapıştırmayın: her bölüm ve her anahtar dosyada yalnız bir kez geçebilir. Tek
platform kullanıyorsanız ötekinin bölümünü silin.

**Google Ads**

1. Google Cloud'da bir proje açın, **Google Ads API**'yi etkinleştirin (APIs & Services → Library).
2. Projenin **Google Ads API Overview** sayfasında erişim düzeyine bakın. Yeni projeler **Test**
   düzeyinde başlar ve yalnız test hesaplarına ulaşır; gerçek hesap için **Upgrade access level** →
   **Explorer** başvurusunu yapın. Google, Explorer'ı başvurudan hemen sonra verebilir. Explorer
   verilene kadar gerçek hesaba yapılan her çağrı `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` hatası
   verir. 9 Eylül 2026'dan beri geliştirici token'ı (developer token) artık kullanılmıyor; erişim
   düzeyi Cloud projesine bağlı.
3. OAuth onay ekranını `https://www.googleapis.com/auth/adwords` kapsamıyla kurun, **Desktop app**
   türünde bir istemci oluşturup JSON dosyasını indirin, sonra uygulamayı yayınlayın (**Publish
   app**). Uygulama *Testing* durumunda kalırsa yenileme token'ı 7 gün sonra geçersiz olur.
4. Yenileme token'ını `gcloud auth application-default login` ile alın; değerleri ekrana dökmeden
   `~/google-ads.yaml` dosyasına yazıp `chmod 600` yapın (komutlar rehberde). Bu dosyayı Google'ın
   kendi istemci kütüphaneleri de okur: dosya zaten varsa üzerine yazmayın, yalnız üç anahtarı
   güncelleyin (rehberdeki betik böyle yapar).
5. Ayar dosyasının `[google]` bölümünde `customer_id` satırının başındaki `#` işaretini kaldırıp
   hesabınızı, `max_daily_budget` satırına da kendi üst sınırınızı yazın. Google Ads arayüzünde hesap bütçesine (account budget),
   otomatik uygulamaya (auto-apply) ve dönüşüm hedeflerine de bir göz atın: hesap bütçesi dolunca
   promosyon kredisi beklese bile bütün kampanyalar durur.

**Meta**

1. developers.facebook.com'da bir uygulama açıp **Create & manage ads with Marketing API**
   kullanım senaryosunu ekleyin. "Create & manage app ads" senaryosu `ads_management` ve `ads_read`
   izinlerini getirmiyor (gözlem, Eylül 2026).
2. Bu iki izni ekleyin (gerekirse tek tek). Sayfa ve Instagram denetimleri için
   `business_management`, `pages_show_list` ve `pages_read_engagement` de ekleyebilirsiniz.
3. **App settings → Basic** bölümüne gizlilik politikası adresini, kategoriyi ve simgeyi girip
   uygulamayı **Live** moduna alın. Geliştirme modundaki bir uygulamayla oluşturulan her reklam öğesi
   (creative) `100/1885183` hatasıyla reddedilir (gözlem, Ekim 2026).
4. Önce token'ı alacak kullanıcıya reklam hesabında yetki verin: Business ayarları → **Accounts** →
   **Ad accounts** → hesabınız → **Assign people** → **Manage campaigns**. Token penceresi yalnız
   kullanıcının zaten erişebildiği varlıkları gösterir. Sonra **Graph API Explorer**'da kullanıcı
   token'ı üretin: izin kutusuna `ads_management` ve `ads_read`'i (isteğe bağlı olanları da) yazıp
   **Generate Access Token**'a basın; açılan pencerede hem Facebook sayfasını hem Instagram hesabını
   işaretleyin. En son **Access Token Debugger → Extend Access Token** ile yaklaşık 60 gün geçerli
   token'a çevirin.
5. Token'ı saklayın. macOS Anahtar Zinciri için önce aşağıdaki satırı yazın, sonra token'ı
   kopyalayın, en son Enter'a basın; ardından panoyu `pbcopy </dev/null` ile temizleyin:

   ```bash
   security add-generic-password -U -a "$USER" -s adops-guard-meta -w "$(pbpaste)"
   ```

   `-w`'yi boş bırakırsanız `security` token'ı kendisi sorar ve 128 karakterde sessizce keser; Meta
   token'ları 200 karakter kadardır. Bu satırı başka satırlarla birlikte de yapıştırmayın: panoda
   komutun kendisi olur ve token yerine o kaydedilir.

   Sonra ayar dosyasının `[meta]` bölümünde `token_source = keychain` ve
   `keychain_service = adops-guard-meta` satırlarının başındaki `#` işaretini kaldırın. Token'ı
   yalnız bu oturumda kullanacaksanız `read -rs META_ACCESS_TOKEN && export META_ACCESS_TOKEN`
   yeterli; o zaman ayar dosyasında bir şey değiştirmeniz gerekmez.
6. `[meta]` bölümünde `ad_account_id` satırının başındaki `#` işaretini kaldırıp reklam
   hesabınızı, üst sınırlara da kendi rakamlarınızı yazın. Hesabın toplam harcamasına Ads Manager → **Billing & payments** → **Account
   spending limit** ile bir tavan koyun; adops-guard bu limiti gösterir ama değiştirmez. Uygulama
   tanıtımı (App Install) yapacaksanız iOS platformunu ekleyin ve uygulamayı Business ayarlarından
   reklam hesabına bağlayın.

**Denetim**

```bash
adops-guard doctor                         # her şeyi sırayla ve yalnız okuyarak denetler
adops-guard doctor meta --app 1234567890   # uygulama bu reklam hesabından tanıtılabiliyor mu?
```

Her ✗ satırının altında ne yapılacağı yazar: menü yolu, izin adı ya da komut. Henüz kurmadığınız
platform atlanır. Bağlantı hatası ya da platformdan gelen 5xx yanıtı kurulum hatası sayılmaz; araç
bunu ayrıca söyler. Gizli değerler hiçbir zaman ekrana basılmaz; hiçbir denetim başarısız olmadıysa
çıkış kodu 0 olur. Bütün hata kodları ve çözümleri:
[docs/troubleshooting.md](docs/troubleshooting.md).

Meta kullanıcı token'ı yaklaşık 60 günde, *Testing* durumundaki bir Google uygulamasının yenileme
token'ı 7 günde biter. Yenileme tarihini takviminize not edin.

## Hızlı başlangıç

```bash
pipx install "git+https://github.com/deegitech/adops-guard.git@v0.1.0"
mkdir -p ~/adops && cd ~/adops
curl -fsSLO https://raw.githubusercontent.com/deegitech/adops-guard/v0.1.0/examples/adops-guard.example.ini
mv adops-guard.example.ini adops-guard.ini
chmod go-w adops-guard.ini                   # başkalarının değiştirebildiği ayar dosyası reddedilir
# sonra dosyada customer_id ve ad_account_id satırlarının başındaki # işaretini kaldırıp kendi
# değerlerinizi, üst sınırlara da kendi rakamlarınızı yazın
if [ -e ~/google-ads.yaml ]; then            # Google'ın kütüphaneleri de bu dosyayı kullanır
  echo "~/google-ads.yaml var: client_id, client_secret ve refresh_token anahtarlarını içine ekleyin"
else
  (umask 077 && curl -fsSL -o ~/google-ads.yaml \
    https://raw.githubusercontent.com/deegitech/adops-guard/v0.1.0/examples/google-ads.example.yaml)
fi
chmod 600 ~/google-ads.yaml                  # REPLACE_ME yazan yerlere kendi değerlerinizi yazın
read -rs META_ACCESS_TOKEN && export META_ACCESS_TOKEN   # yapıştırın, Enter: ekranda ve geçmişte görünmez

adops-guard doctor                                                          # kurulum denetimi
adops-guard google status
adops-guard google budget set --campaign 11111111111 --amount 25            # deneme
adops-guard google budget set --campaign 11111111111 --amount 25 --apply    # gerçek
adops-guard meta status
```

Kurulumun bütün adımları [docs/setup.md](docs/setup.md), hatalar ve çözümleri
[docs/troubleshooting.md](docs/troubleshooting.md) dosyasında.

Hatalar, güvenlik açıkları ve katkılar için: [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md).

Lisans: MIT. Geliştiren: [DEEGITECH](https://github.com/deegitech).
