# Playlist Sync — Serato DJ Pro ⇄ rekordbox

Serato DJ Pro 와 rekordbox 의 **플레이리스트를 양방향으로 정확히 동기화**하는 무료 오픈소스 도구입니다 (비상업적, MIT).
Two-way playlist sync between Serato DJ Pro crates and rekordbox playlists. Free, non-commercial, MIT.

왼쪽 = Serato DJ Pro 화면 스타일, 오른쪽 = rekordbox 화면 스타일. 가운데 → ← 버튼으로 선택한 플레이리스트를 보냅니다.
Left pane = Serato-style, right pane = rekordbox-style; the centre arrows send the selected playlist.

## 실행 / Run

```bash
python3 -m pip install --user pyrekordbox      # 한 번만 / once
python3 playlist_sync.py                        # 브라우저가 열립니다 / opens your browser
```

* Python 3.8+, 단일 파일(`playlist_sync.py`), 외부 의존성은 `pyrekordbox` 하나입니다.
* **Serato DJ Pro 와 rekordbox 를 모두 종료**한 뒤 동기화하세요 (실행 중이면 앱이 거부합니다).
* 서버는 `127.0.0.1` 에서만 열리며 토큰으로 보호됩니다. 데이터는 밖으로 나가지 않습니다.

옵션 / options:

```bash
python3 playlist_sync.py --selftest                       # 환경 점검
python3 playlist_sync.py --plan                           # 동기화 계획만 출력
python3 playlist_sync.py --serato "/path/_Serato_" --rekordbox "/path/master.db"
```

## 동작 / How it works

* 곡의 동일성은 **파일 경로**(유니코드 NFC, macOS/Windows 는 대소문자 무시)로 판단하고, **순서와 중복**까지 그대로 옮깁니다.
* 마지막 동기화 상태(`~/.playlist_sync/state.json`)와 비교하는 3-way 방식: 한쪽만 바뀌면 그 방향으로, 양쪽이 바뀌면 "충돌"로 표시하고 기본값은 최근 수정된 쪽입니다(직접 바꿀 수 있음).
* Serato 하위 크레이트(`Parent%%Child`) ⇄ rekordbox 폴더 구조를 서로 대응시킵니다.
* **삭제는 절대 자동으로 전파되지 않습니다.** 사용자가 동작 드롭다운에서 직접 선택해야 합니다.
* 쓰기 전 자동 백업: `~/.playlist_sync/backups/<시각>` (최근 30개 유지). rekordbox 쓰기는 하나의 트랜잭션이며 오류 시 롤백, 쓴 뒤 디스크에서 다시 읽어 검증합니다.

## 한계 / Known limitations (정직하게)

* Serato 쪽에서 `database V2` 에 없는 곡을 크레이트에 넣으면 Serato 가 열어주지만 **"unanalyzed"** 로 보이고 아티스트/BPM/키가 비어 있습니다. 곡을 선택해 *Analyze Files* 를 실행하세요. (이 도구는 Serato 의 `database V2` 를 수정하지 않습니다.)
* rekordbox 에 새로 추가되는 곡만 메타데이터(제목·아티스트·BPM·키·코멘트 등)를 Serato 에서 가져옵니다. 이미 있는 곡의 메타데이터는 동기화하지 않습니다.
* 기본 Serato 라이브러리(`~/Music/_Serato_`)만 지원합니다. 외장 드라이브의 `_Serato_` 는 `--serato` 로 지정할 수 있으나 검증하지 않았습니다.
* 하위 크레이트를 가진 크레이트에 곡이 함께 들어 있으면 동기화하지 않습니다(rekordbox 폴더에는 곡을 둘 수 없음). 스마트 플레이리스트와 `CUE Analysis Playlist` 는 무시합니다.
* 디스크에 없는 파일은 rekordbox 로 새로 가져오지 않고 경고와 함께 건너뜁니다.
* 검증 환경: macOS 의 실제 Serato DJ Pro / rekordbox 7 에서 이 도구가 쓴 결과를 열어 확인했습니다. Windows 는 검증하지 않았습니다. 이 도구는 Pioneer / Serato 와 무관한 비공식 도구입니다.
* **백업을 확인한 뒤 사용하세요.** 보증은 없습니다 (MIT).

## 개발 / Development

```bash
python3 build.py        # src/*.py + ui/index.html -> playlist_sync.py
```

`tests/` 의 테스트는 실제 Serato/rekordbox 라이브러리 복사본(`/mnt/user-data/uploads/{_Serato_,rekordbox}`)을 사용하도록 작성되어 개인 라이브러리 없이는 실행되지 않습니다. 비공개 라이브러리는 저장소에 포함하지 않습니다.

## 라이선스 / License

MIT — 비상업적으로 누구나 자유롭게 사용·수정·배포할 수 있습니다.
