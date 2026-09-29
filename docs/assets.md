# 3D 에셋 제작 기록 (Higgsfield)

대시보드와 README에 쓰인 카피바라 캐릭터·아이콘·모션은 모두 Higgsfield MCP로 만들었습니다. 같은 스타일로 캐릭터를 추가하거나 다시 만들 때 아래 프롬프트를 그대로 쓰면 됩니다.

## 캐릭터 설정

| 에이전트 | 별명 | 의상·소품 | 색 |
|---|---|---|---|
| 총괄 에이전트 (Orchestrator) | 유자 디렉터 | 인디고·바이올렛 블레이저(금색 트림, 별 배지), 헤드셋 마이크, 머리 위 유자, 지휘봉, 홀로그램 플로차트 태블릿 | `#6D5EF5` |
| 리서치 에이전트 (Researcher) | 돋보기 탐험가 | 틸·시안 탐험 조끼, 이마에 올린 고글, 커다란 돋보기, 떠다니는 홀로그램 데이터 카드(막대그래프·지구본) | `#14B8A6` |
| 검수 에이전트 (Reviewer) | 꼼꼼 검수관 | 코랄·앰버 카디건, 금테 동그란 안경, 체크리스트 클립보드, 체크 표시 도장 | `#FF7A59` |

## 제작 순서

1. **라인업 한 장 먼저** (`gpt_image_2_5`, quality high, 2k, 16:9) — 세 캐릭터를 한 장에 그려 스타일을 고정합니다.
2. **개별 캐릭터** (`gpt_image_2_5`, 1:1) — 라인업 이미지를 `image_references`로 넣고 "왼쪽/가운데/오른쪽 캐릭터만 단독으로" 요청해 얼굴·의상을 그대로 유지합니다.
3. **배경 제거** (`remove_background`) — 대시보드 오버레이와 3D 변환용 컷아웃.
4. **모션 루프** (`seedance_2_5`, mode `omni_reference`, 5초, 720p, 오디오 없음) — 같은 이미지를 `start_image`와 `end_image`에 모두 넣으면 첫 프레임과 마지막 프레임이 거의 같아져 끊김 없는 루프가 됩니다.
5. **3D 메시** (`image_to_3d`, 텍스처 + PBR, 30,000 폴리곤) — 컷아웃 이미지를 GLB로 변환해 대시보드의 "3D로 보기"에서 돌려 볼 수 있습니다.
6. **채널 아이콘·스튜디오 배경** (`gpt_image_2_5`) — 아이콘은 `background: transparent`로 투명 배경을 바로 받았습니다. 플랫폼 로고는 넣지 않고 색과 사물로만 표현했습니다.

웹용 변환: 이미지는 Pillow로 WebP(품질 82~85), 영상은 ffmpeg(H.264 main, CRF 25, faststart, 오디오 제거)로 줄였습니다.

## 프롬프트

### 공통 스타일

```
Stylized 3D character render in a premium vinyl-toy style: smooth rounded forms, soft satin and glossy
materials, subtle emissive glow on the holograms, soft three-point studio lighting with gentle rim light,
soft ambient occlusion, high detail, cohesive modern palette, Pixar-quality 3D render.
No text, no letters, no logos, no watermark.
```

### 라인업

```
Character lineup of three adorable anthropomorphic capybara characters standing side by side on a softly lit
round glossy platform, full body, front three-quarter view, generous spacing, plain deep navy gradient studio
background (#0B1220 fading to #1E293B). Each capybara has the classic calm, chill capybara face: rounded barrel
body, short legs, small rounded ears, big gentle dark eyes with soft highlights, a relaxed content smile, soft
warm brown fur (#A0703F to #C8925A) rendered as a smooth stylized vinyl-toy surface with subtle fur texture,
standing upright like a mascot.
LEFT: the Research agent capybara, wearing a teal and cyan (#14B8A6, #22D3EE) explorer vest with white piping and
small explorer goggles pushed up on its head, holding a large magnifying glass, with two small translucent cyan
holographic data cards (a bar chart and a globe) floating beside it.
CENTER: the Orchestrator / director capybara, slightly taller, wearing a deep indigo and violet (#4F46E5, #7C3AED)
blazer with warm gold trims and a tiny gold star badge, a slim headset microphone, a small round yuzu fruit
balanced on its head, holding a glowing conductor baton in one paw and a small holographic flowchart tablet in
the other.
RIGHT: the Review / QA capybara, wearing a coral and amber (#F97316, #FB7185) cardigan and round gold-rimmed
glasses, holding a clipboard with a green checklist and a large rubber stamp with a check mark.
+ 공통 스타일
```

### 개별 캐릭터 (라인업을 image_references로)

```
Using the reference image, render ONLY the CENTER capybara (…의상·소품 설명…) completely alone. Keep its face,
fur, proportions, outfit, colors and props identical to the reference. Full body standing, centered, front
three-quarter view, standing on a small glowing round disc, the character occupies about 70% of the frame height
with generous margin on all sides. Background: smooth deep navy studio gradient (#101a33 softly lit center fading
to #0B1220 edges), soft spotlight from above, no other objects. + 공통 스타일
```

### 모션 루프 (예: 총괄)

```
The director capybara in the indigo blazer stays centered on its glowing disc and conducts calmly: it waves the
glowing baton in smooth graceful arcs, the holographic flowchart on its tablet pulses as its nodes light up one
by one, it nods gently, blinks, its ears twitch and the little yuzu on its head wobbles. Relaxed, confident
capybara charm, soft breathing motion. Subtle floating light particles. Locked-off static camera, identical
framing and background throughout, the character returns to its exact starting pose at the end for a seamless
loop. Smooth stylized 3D animation.
```

리서치 에이전트는 "돋보기로 좌우를 훑고 홀로그램 카드가 떠다니며 막대그래프가 올라가는" 동작, 검수 에이전트는 "클립보드의 체크 표시가 하나씩 켜지고 안경을 고쳐 쓴 뒤 도장을 한 번 찍는" 동작으로 같은 구조의 프롬프트를 썼습니다.

## 파일 위치

`web/assets/manifest.json`이 모든 에셋 경로를 가리킵니다. 대시보드는 이 파일만 읽으므로 캐릭터를 바꿀 때는 파일을 교체하고 manifest 경로만 맞추면 됩니다.
