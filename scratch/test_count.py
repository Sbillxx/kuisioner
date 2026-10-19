import re
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    page.set_content('''
        <table>
            <thead>
                <tr><th>No</th><th>Dosen</th><th>Matakuliah</th><th>Jenis</th></tr>
            </thead>
            <tbody>
                <tr><td>1</td><td>Dosen 1</td><td>Matkul 1</td><td><button class="btn btn-primary">Teori</button></td></tr>
                <tr><td>2</td><td>Dosen 2</td><td>Matkul 2</td><td><button class="btn btn-primary">Teori</button></td></tr>
                <tr><td>3</td><td>Dosen 3</td><td>Matkul 3</td><td><button class="btn btn-primary">Teori</button></td></tr>
                <tr><td>4</td><td>Dosen 4</td><td>Matkul 4</td><td><button class="btn btn-primary">Teori</button><button class="btn btn-warning">Praktikum</button></td></tr>
                <tr><td>5</td><td>Dosen 5</td><td>Matkul 5</td><td><button class="btn btn-primary">Teori</button></td></tr>
                <tr><td>6</td><td>Dosen 6</td><td>Matkul 6</td><td><button class="btn btn-primary">Teori</button></td></tr>
                <tr><td>7</td><td>Dosen 7</td><td>Matkul 7</td><td><button class="btn btn-primary">Teori</button></td></tr>
                <tr><td>8</td><td>Dosen 8</td><td>Matkul 8</td><td><button class="btn btn-primary">Teori</button></td></tr>
            </tbody>
        </table>
    ''')
    
    loc1 = page.locator('table tr td:last-child').locator('button, a, .btn').filter(has_text=re.compile(r"teori|praktik|isi|kuesioner", re.I))
    print("Loc 1 count:", loc1.count())
    
    # What if button is found simply by button.btn or button:has-text("Teori"), button:has-text("Praktikum")?
    loc2 = page.locator('button, a, .btn').filter(has_text=re.compile(r"^\s*(?:teori|praktik|praktikum)\s*$", re.I))
    print("Loc 2 count:", loc2.count())
    
    b.close()
