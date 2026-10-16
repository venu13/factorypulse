# Sample images

The Visual QA Lab's **Sample gallery** lists every `.png`, `.jpg`, `.jpeg` and `.webp` file in this folder
(next to the built-in *synthetic* weld / gear / bearing renders). Drop real defect photos here and restart the app.

## Suggested public datasets
| Dataset | Content | Where |
|---|---|---|
| **NEU Surface Defect Database** | 1,800 grayscale steel-surface images, 6 classes (crazing, inclusion, patches, pitted surface, rolled-in scale, scratches) | http://faculty.neu.edu.cn/songkechen/zh_CN/zdylm/263270/list/ (also mirrored on Kaggle as "NEU surface defect database") |
| **MVTec AD** | 15 industrial object/texture categories with real defects (e.g. metal nut, screw, grid) | https://www.mvtec.com/company/research/datasets/mvtec-ad |

Both are licensed for **non-commercial / research use** - check the licence before redistributing images,
and do **not** commit the datasets to this repo (keep only a handful of examples you are allowed to share).

## Quick start
```bash
# NEU: copy a few images from the extracted archive, e.g.
cp NEU-DET/IMAGES/pitted_surface_1.jpg sample_images/
# MVTec AD: copy defective test images
cp mvtec_ad/metal_nut/test/scratch/000.png sample_images/metal_nut_scratch.png
streamlit run app.py     # QA Lab -> "Sample gallery"
```
Images larger than 2048 px on a side are downscaled before being sent to Gemini. Gemini returns
bounding boxes as `[ymin, xmin, ymax, xmax]` normalized to 0-1000. Real photos have no ground-truth boxes embedded,
so in offline mock mode they get a clearly-labelled pseudo-analysis; use live Gemini for real results.
