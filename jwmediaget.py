#!/usr/bin/env python3
import os, sys, json, sqlite3, zipfile, tempfile, shutil, time, threading
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk

try:
    import customtkinter as ctk
except ImportError:
    os.system("pip install customtkinter -q")
    import customtkinter as ctk

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    os.system("pip install requests -q")
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

JW_PURPLE       = "#5b2d8e"
JW_PURPLE_DARK  = "#4a2373"
JW_PURPLE_LIGHT = "#ede8f5"
JW_BG           = "#f4f4f4"
JW_WHITE        = "#ffffff"
JW_BORDER       = "#e0e0e0"
JW_TEXT         = "#1a1a1a"
JW_TEXT_MUTED   = "#999999"
JW_GREEN        = "#2e7d32"
JW_RED          = "#c62828"

LANG_IDX_TO_CODE = {0: "E", 474: "RSL"}

def lang_code(idx):
    return LANG_IDX_TO_CODE.get(int(idx), "E") if idx is not None else "E"

def make_session():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    r = Retry(total=5, backoff_factor=2, status_forcelist=[429,500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=r))
    return s

def extract_db(jwpub_path, tmpdir):
    with zipfile.ZipFile(jwpub_path) as z:
        z.extract("contents", tmpdir)
    with zipfile.ZipFile(os.path.join(tmpdir, "contents")) as z:
        dbs = [f for f in z.namelist() if f.endswith(".db")]
        if not dbs: raise RuntimeError("База данных не найдена")
        z.extract(dbs[0], tmpdir)
    return os.path.join(tmpdir, dbs[0])

def collect_videos(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    seen, result = set(), []
    def add(label, sym, fp, track, lang, issue, source, docid=None, booknum=None):
        key = (sym, track, lang, issue, docid)
        if key in seen or not fp: return
        seen.add(key)
        result.append(dict(label=(label or "").replace('\xa0',' ').strip(),
            symbol=sym, filepath=fp, track=track, lang_idx=lang,
            issue=issue or "", source=source, docid=docid, booknum=booknum))
    # Для Библии (nwt) нужен booknum — получаем через BibleBook
    try:
        cur.execute("""
            SELECT m.Label, m.KeySymbol, m.FilePath, m.Track, m.MepsLanguageIndex,
                   m.IssueTagNumber, m.MepsDocumentId, bb.BibleBookId
            FROM Multimedia m
            LEFT JOIN Document d ON d.MepsDocumentId = m.MepsDocumentId
                AND d.MepsLanguageIndex = m.MepsLanguageIndex
            LEFT JOIN BibleBook bb ON bb.BookDocumentId = d.DocumentId
            WHERE m.MajorType=2 AND m.FilePath!=''
            ORDER BY m.MepsDocumentId, m.Track
        """)
        for label,sym,fp,track,lang,issue,docid,booknum in cur.fetchall():
            add(label, sym, fp, track, lang, issue, "урок", docid, booknum)
    except Exception:
        cur.execute("SELECT Label,KeySymbol,FilePath,Track,MepsLanguageIndex,IssueTagNumber,MepsDocumentId FROM Multimedia WHERE MajorType=2 AND FilePath!='' ORDER BY MepsDocumentId,Track")
        for label,sym,fp,track,lang,issue,docid in cur.fetchall():
            add(label, sym, fp, track, lang, issue, "урок", docid)
    try:
        cur.execute("""
            SELECT DISTINCT m.Label, m.KeySymbol, m.FilePath, m.Track, m.MepsLanguageIndex,
                   m.IssueTagNumber, m.MepsDocumentId, bb.BibleBookId
            FROM ExtractMultimedia m
            LEFT JOIN Document d ON d.MepsDocumentId = m.MepsDocumentId
                AND d.MepsLanguageIndex = m.MepsLanguageIndex
            LEFT JOIN BibleBook bb ON bb.BookDocumentId = d.DocumentId
            WHERE m.MajorType=2 AND m.FilePath!=''
            ORDER BY m.KeySymbol, m.IssueTagNumber, m.Track
        """)
        for label,sym,fp,track,lang,issue,docid,booknum in cur.fetchall():
            add(label, sym, fp, track, lang, issue, "материал", docid, booknum)
    except Exception:
        cur.execute("SELECT DISTINCT Label,KeySymbol,FilePath,Track,MepsLanguageIndex,IssueTagNumber,MepsDocumentId FROM ExtractMultimedia WHERE MajorType=2 AND FilePath!='' ORDER BY KeySymbol,IssueTagNumber,Track")
        for label,sym,fp,track,lang,issue,docid in cur.fetchall():
            add(label, sym, fp, track, lang, issue, "материал", docid)
    conn.close()
    return result

def api_get_url(session, pub, track, langwritten, quality, issue="", filepath="", docid=None, booknum=None):
    # Для Библии (nwt): используем pub+booknum+track, НЕ docid
    if pub and booknum:
        for fmt in ["MP4", "M4V"]:
            try:
                r = session.get("https://app.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS",
                    params=dict(langwritten=langwritten, pub=pub, booknum=booknum,
                                track=track, fileformat=fmt),
                    timeout=30)
                r.raise_for_status()
                data = r.json()
                variants = data.get("files", {}).get(langwritten, {}).get(fmt)
                if not variants: continue
                target = f"{quality}p"
                chosen = next((v for v in variants if v.get("label") == target), None)
                if not chosen:
                    chosen = sorted(variants, key=lambda x: {"240p":1,"360p":2,"480p":3,"720p":4}.get(x.get("label",""),0), reverse=True)[0] if variants else None
                if not chosen: continue
                url = chosen["file"]["url"]
                fname = url.split("/")[-1]
                api_pub = chosen.get("pub", pub)
                api_booknum = chosen.get("booknum", booknum)
                return url, fname, api_pub, api_booknum
            except Exception:
                continue
        return None, "booknum не найден", "", 0

    # Если есть docid — используем его
    if docid:
        for fmt in ["MP4", "M4V"]:
            try:
                r = session.get("https://app.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS",
                    params=dict(langwritten=langwritten, docid=docid, fileformat=fmt, track=track),
                    timeout=30)
                r.raise_for_status()
                data = r.json()
                variants = data.get("files", {}).get(langwritten, {}).get(fmt)
                if not variants: continue
                target = f"{quality}p"
                chosen = next((v for v in variants if v.get("label") == target), None)
                if not chosen:
                    chosen = sorted(variants, key=lambda x: {"240p":1,"360p":2,"480p":3,"720p":4}.get(x.get("label",""),0), reverse=True)[0] if variants else None
                if not chosen: continue
                url = chosen["file"]["url"]
                fname = url.split("/")[-1]
                api_pub   = chosen.get("pub", "")
                api_booknum = chosen.get("booknum", 0)
                return url, fname, api_pub, api_booknum
            except Exception:
                continue
        if not pub:
            return None, "docid не найден", "", 0

    # Обычные публикации — pub + track
    params_base = dict(langwritten=langwritten, pub=pub, track=track)
    if issue: params_base["issue"] = issue
    for fmt in ["MP4", "M4V"]:
        params = {**params_base, "fileformat": fmt}
        try:
            r = session.get("https://app.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            variants = data.get("files", {}).get(langwritten, {}).get(fmt)
            if not variants: continue
        except Exception:
            continue
        target = f"{quality}p"
        chosen = next((v for v in variants if v.get("label") == target), None)
        if not chosen:
            chosen = sorted(variants, key=lambda x: {"240p":1,"360p":2,"480p":3,"720p":4}.get(x.get("label",""),0), reverse=True)[0] if variants else None
        if not chosen: continue
        url = chosen["file"]["url"]
        api_pub     = chosen.get("pub", pub)
        api_booknum = chosen.get("booknum", 0)
        return url, url.split("/")[-1], api_pub, api_booknum
    return None, "формат не найден", "", 0

def download_file(url, dest, session, progress_cb=None):
    r = session.get(url, stream=True, timeout=180)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    tmp = dest + ".part"
    done = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk); done += len(chunk)
                if progress_cb and total: progress_cb(done / total)
        os.replace(tmp, dest)
    except:
        if os.path.exists(tmp): os.remove(tmp)
        raise


# ── Красивый переключатель ─────────────────────────────────────────────────

class Selector(ctk.CTkFrame):
    """Горизонтальный переключатель с белым текстом на выбранной кнопке."""
    def __init__(self, parent, choices, callback=None, **kw):
        super().__init__(parent, fg_color=JW_PURPLE_LIGHT, corner_radius=8, **kw)
        self._choices  = choices
        self._callback = callback
        self._value    = choices[0][1]
        self._btns     = {}
        inner = tk.Frame(self, bg=JW_PURPLE_LIGHT)
        inner.pack(fill="both", expand=True, padx=4, pady=4)
        for i, (label, val) in enumerate(choices):
            inner.columnconfigure(i, weight=1)
            b = ctk.CTkButton(inner, text=label,
                              height=34,
                              font=ctk.CTkFont("Segoe UI", 12, "bold"),
                              corner_radius=6,
                              border_width=0,
                              command=lambda v=val: self._select(v))
            b.grid(row=0, column=i, padx=3, pady=0, sticky="ew")
            self._btns[val] = b
        self._select(self._value, notify=False)

    def _select(self, val, notify=True):
        self._value = val
        for v, b in self._btns.items():
            if v == val:
                b.configure(fg_color=JW_PURPLE, hover_color=JW_PURPLE_DARK,
                            text_color="#ffffff")
            else:
                b.configure(fg_color=JW_PURPLE_LIGHT, hover_color="#ddd4f0",
                            text_color=JW_PURPLE)
        if notify and self._callback:
            self._callback(val)

    def get(self): return self._value


# ── Главное окно ───────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("JWMediaGet")
        self.geometry("520x640")
        self.minsize(420, 540)
        self.resizable(True, True)
        self.configure(fg_color=JW_BG)

        self._real_path = None
        self._stop_flag = False
        self._running   = False
        self.output_path = tk.StringVar(value=str(Path.home() / "Videos" / "JWLibrary"))

        self._build()

    def _build(self):
        # Шапка
        header = ctk.CTkFrame(self, fg_color=JW_PURPLE, corner_radius=0, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="JWMediaGet",
                     font=ctk.CTkFont("Segoe UI", 17, "bold"),
                     text_color="#ffffff").pack(side="left", padx=20)
        ctk.CTkLabel(header, text="загрузка видео из .jwpub",
                     font=ctk.CTkFont("Segoe UI", 11),
                     text_color="#c4a8e8").pack(side="left")

        ctk.CTkFrame(self, fg_color=JW_BORDER, height=1, corner_radius=0).pack(fill="x")

        # Тело
        body = ctk.CTkScrollableFrame(self, fg_color=JW_BG,
                                       scrollbar_button_color=JW_BORDER,
                                       scrollbar_button_hover_color="#ccbbee")
        body.pack(fill="both", expand=True)
        p = ctk.CTkFrame(body, fg_color="transparent")
        p.pack(fill="both", expand=True, padx=24, pady=18)

        # Файл
        self._sec(p, "Файл публикации")
        fc = self._card(p)
        fr = ctk.CTkFrame(fc, fg_color="transparent")
        fr.pack(fill="x", padx=14, pady=12)
        fr.columnconfigure(0, weight=1)
        self._file_lbl = ctk.CTkLabel(fr, text="Файл не выбран",
                                       text_color=JW_TEXT_MUTED, anchor="w",
                                       font=ctk.CTkFont("Segoe UI", 12))
        self._file_lbl.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(fr, text="Выбрать", width=100, height=32,
                      font=ctk.CTkFont("Segoe UI", 11, "bold"),
                      fg_color=JW_PURPLE, hover_color=JW_PURPLE_DARK,
                      corner_radius=6,
                      command=self._pick_file).grid(row=0, column=1, padx=(10,0))

        # Папка
        self._sec(p, "Папка сохранения")
        fc2 = self._card(p)
        fr2 = ctk.CTkFrame(fc2, fg_color="transparent")
        fr2.pack(fill="x", padx=14, pady=12)
        fr2.columnconfigure(0, weight=1)
        ctk.CTkLabel(fr2, textvariable=self.output_path,
                     text_color=JW_TEXT, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 12)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(fr2, text="Изменить", width=100, height=32,
                      font=ctk.CTkFont("Segoe UI", 11, "bold"),
                      fg_color=JW_PURPLE, hover_color=JW_PURPLE_DARK,
                      corner_radius=6,
                      command=self._pick_output).grid(row=0, column=1, padx=(10,0))

        # Качество
        self._sec(p, "Качество видео")
        qc = self._card(p)
        self._quality = Selector(qc,
            [("240p","240"),("360p","360"),("480p","480"),("720p","720")])
        self._quality.pack(fill="x", padx=10, pady=10)

        # Что скачивать
        self._sec(p, "Что скачивать")
        sc = self._card(p)
        self._src = Selector(sc,
            [("Всё","all"),("Только уроки","lessons"),("Доп. материалы","refs")])
        self._src.pack(fill="x", padx=10, pady=10)

        # Кнопка
        self.start_btn = ctk.CTkButton(p, text="Начать загрузку",
                                        height=46,
                                        font=ctk.CTkFont("Segoe UI", 14, "bold"),
                                        fg_color=JW_PURPLE,
                                        hover_color=JW_PURPLE_DARK,
                                        corner_radius=8,
                                        command=self._start)
        self.start_btn.pack(fill="x", pady=(16,6))

        # Статус + прогресс
        self.status_lbl = ctk.CTkLabel(p, text="",
                                        text_color=JW_TEXT_MUTED,
                                        font=ctk.CTkFont("Segoe UI", 11),
                                        anchor="w")
        self.status_lbl.pack(fill="x", pady=(2,4))

        self.bar = ctk.CTkProgressBar(p, height=4, corner_radius=2,
                                       fg_color=JW_BORDER,
                                       progress_color=JW_PURPLE)
        self.bar.pack(fill="x", pady=(0,14))
        self.bar.set(0)

        # Лог
        self._sec(p, "Лог загрузки")
        self.log = ctk.CTkTextbox(p, height=170,
                                   font=ctk.CTkFont("Consolas", 10),
                                   fg_color=JW_WHITE,
                                   text_color="#666666",
                                   border_color=JW_BORDER,
                                   border_width=1,
                                   corner_radius=6,
                                   state="disabled")
        self.log.pack(fill="both", expand=True)

    def _sec(self, parent, text):
        ctk.CTkLabel(parent, text=text,
                     font=ctk.CTkFont("Segoe UI", 12, "bold"),
                     text_color=JW_TEXT, anchor="w").pack(fill="x", pady=(12,4))

    def _card(self, parent):
        c = ctk.CTkFrame(parent, fg_color=JW_WHITE, corner_radius=8,
                          border_width=1, border_color=JW_BORDER)
        c.pack(fill="x", pady=(0,2))
        return c

    def _pick_file(self):
        p = filedialog.askopenfilename(filetypes=[("JW Publication","*.jwpub"),("Все","*.*")])
        if p:
            self._real_path = p
            self._file_lbl.configure(text=Path(p).name, text_color=JW_TEXT)
            self._log(f"Файл: {Path(p).name}")

    def _pick_output(self):
        p = filedialog.askdirectory()
        if p: self.output_path.set(p)

    def _log(self, txt):
        self.log.configure(state="normal")
        self.log.insert("end", txt + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self):
        if not self._real_path:
            messagebox.showwarning("", "Сначала выбери .jwpub файл")
            return
        if self._running: return
        self._running = True
        self._stop_flag = False
        self.start_btn.configure(text="Остановить",
                                  fg_color=JW_RED, hover_color="#b71c1c",
                                  command=self._stop)
        self.bar.set(0)
        threading.Thread(target=self._run, daemon=True).start()

    def _stop(self):
        self._stop_flag = True

    def _run(self):
        jwpub   = self._real_path
        outdir  = self.output_path.get()
        quality = self._quality.get()
        src     = self._src.get()
        os.makedirs(outdir, exist_ok=True)

        log_path = os.path.join(outdir, ".log.json")
        try:
            with open(log_path, encoding="utf-8") as f: saved = json.load(f)
        except: saved = {}

        self.after(0, lambda: self.status_lbl.configure(text="Читаю файл..."))
        try:
            tmp = tempfile.mkdtemp()
            db  = extract_db(jwpub, tmp)
            vids = collect_videos(db)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
            self._finish(); return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if src == "lessons": vids = [v for v in vids if v["source"] == "урок"]
        elif src == "refs":  vids = [v for v in vids if v["source"] == "материал"]

        total = len(vids)
        self._log(f"Найдено {total} видео · {quality}p")
        self._log("─" * 36)

        session = make_session()
        ok = skip = err = 0

        for i, v in enumerate(vids):
            if self._stop_flag:
                self._log("Остановлено")
                break

            key = f"{v['symbol']}_{v.get('booknum') or ''}_{v['track']}_{v['lang_idx']}_{v['issue']}"
            lw  = lang_code(v["lang_idx"])

            self.after(0, lambda p=i/total: self.bar.set(p))
            self.after(0, lambda i=i, o=ok, s=skip, e=err:
                self.status_lbl.configure(
                    text=f"Видео {i+1} из {total}  ·  ✓ {o}  ↷ {s}  ✗ {e}",
                    text_color=JW_TEXT_MUTED))

            if saved.get(key) == "ok":
                skip += 1; continue

            url, fname, api_pub, api_booknum = api_get_url(session, v["symbol"], v["track"], lw, quality, v["issue"], v["filepath"], v.get("docid"), v.get("booknum"))
            if url is None:
                err += 1
                self._log(f"✗ {v['label'][:52]}")
                saved[key] = "err"; time.sleep(0.3); continue

            # Логика папки как в JW Library:
            # Некоторые pub — глобальные медиа, они идут в корень
            # Остальные — в папку pub_RSL/
            ROOT_PUBS = {
                'sjj', 'sjjm', 'pk', 'ndl', 'wpc', 'wcgv',
                'jlp', 'jwbcov', 'jwbcov21', 'jwbcov22',
                'jwbcov23', 'jwbcov24', 'jwbcov25',
            }
            sym = api_pub or v["symbol"] or ""
            is_root = (not sym) or (sym.lower() in ROOT_PUBS) or sym.lower().startswith('jwbcov')
            if sym and not is_root:
                lang_suffix = f"_{lw}" if lw != "E" else ""
                pub_folder = os.path.join(outdir, f"{sym}{lang_suffix}")
            else:
                pub_folder = outdir
            os.makedirs(pub_folder, exist_ok=True)

            dest = os.path.join(pub_folder, fname)
            if os.path.exists(dest):
                skip += 1; saved[key] = "ok"; continue

            self._log(f"↓ {v['symbol'] or '?'} / {fname}")
            try:
                download_file(url, dest, session)
                ok += 1; saved[key] = "ok"
            except Exception as e:
                err += 1; saved[key] = "err"
                self._log(f"  ✗ {e}")

            try:
                with open(log_path,"w",encoding="utf-8") as f:
                    json.dump(saved, f, ensure_ascii=False)
            except: pass
            time.sleep(0.2)

        self.after(0, lambda: self.bar.set(1))
        self.after(0, lambda o=ok, s=skip, e=err:
            self.status_lbl.configure(
                text=f"Готово · загружено {o} · пропущено {s} · ошибок {e}",
                text_color=JW_GREEN if not e else "#e65100"))
        self._log("─" * 36)
        self._log(f"Загружено: {ok}   Пропущено: {skip}   Ошибок: {err}")
        self._log(f"Папка: {outdir}")
        if not self._stop_flag:
            self.after(0, lambda o=ok, s=skip, e=err:
                messagebox.showinfo("Загрузка завершена",
                    f"Загружено: {o}\nПропущено: {s}\nОшибок: {e}\n\nПапка:\n{outdir}"))
        self._finish()

    def _finish(self):
        self._running = False
        self.after(0, lambda: self.start_btn.configure(
            text="Начать загрузку", fg_color=JW_PURPLE,
            hover_color=JW_PURPLE_DARK, command=self._start, state="normal"))


if __name__ == "__main__":
    App().mainloop()