import re
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    page.set_content('''
        <div class="card">
            <div class="card-header">KUESIONER</div>
            <table class="table">
                <thead>
                    <tr><th>No</th><th>Dosen</th><th>Matakuliah</th><th>Jenis</th></tr>
                </thead>
                <tbody>
                    <tr><td>1</td><td>• Mila Karmila, M.M.<sup>Teori</sup></td><td>Pengantar Manajemen</td><td><button class="btn btn-primary">Teori</button></td></tr>
                    <tr><td>2</td><td>• Dudu Risana, M.M.<sup>Teori</sup></td><td>Etika dan Hukum Bisnis</td><td><button class="btn btn-primary">Teori</button></td></tr>
                    <tr><td>3</td><td>• Yeni Fitriani Somantri, SE., M.Si.<sup>Teori</sup></td><td>Pengantar Akuntansi II</td><td><button class="btn btn-primary">Teori</button></td></tr>
                    <tr><td>4</td><td>• Arga Sutrisna, SE., M.M.<sup>Teori</sup><br>• Arga Sutrisna, SE., M.M.<sup>Praktikum</sup></td><td>Perpajakan/Lab</td><td><button class="btn btn-primary">Teori</button> <button class="btn btn-warning">Praktikum</button></td></tr>
                    <tr><td>5</td><td>• Ai Kusmiati Asyiah, M.H.<sup>Teori</sup></td><td>Pendidikan Pancasila</td><td><button class="btn btn-primary">Teori</button></td></tr>
                    <tr><td>6</td><td>• Doni Chandra, S.Pd., M.Pd.<sup>Teori</sup></td><td>B. Indonesia</td><td><button class="btn btn-primary">Teori</button></td></tr>
                    <tr><td>7</td><td>• Setji Salgangga, S.M., M.M.<sup>Teori</sup></td><td>Pengantar Ilmu Ekonomi Mikro</td><td><button class="btn btn-primary">Teori</button></td></tr>
                    <tr><td>8</td><td>• Gian Riksa Wibawa, M.M.<sup>Teori</sup></td><td>Matematika Bisnis</td><td><button class="btn btn-primary">Teori</button></td></tr>
                </tbody>
            </table>
        </div>
    ''')

    # Cari tombol kuesioner di luar modal
    kuesioner_table = page.locator('.card:has-text("KUESIONER") table, table').first
    
    # 1. Hitung baris mata kuliah
    rows = kuesioner_table.locator('tbody tr')
    jml_mk = rows.count()
    print("Jumlah Mata Kuliah (baris):", jml_mk)

    # 2. Hitung tombol aksi
    buttons = kuesioner_table.locator('button, a.btn').filter(
        has_text=re.compile(r"^\s*(?:teori|praktik|praktikum)\s*$", re.I)
    )
    jml_tombol = buttons.count()
    print("Jumlah Tombol Kuesioner:", jml_tombol)

    b.close()
