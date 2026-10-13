"""Generates the multi-condition skin classifier notebook."""
import json, io, sys

C = []           # (kind, source)
def md(s): C.append(("markdown", s.strip("\n")))
def code(s): C.append(("code", s.strip("\n")))

md(r"""
# Facial skin condition classifier — Hugging Face transfer learning

Fine-tunes two Hugging Face vision backbones on the same data, compares them,
exports the better one to ONNX, and reports per-class and per-skin-tone
performance.

**Backbones** — `google/vit-base-patch16-224-in21k` (86M) and
`facebook/deit-small-patch16-224` (22M), both Apache-2.0 with documented
provenance. The pretrained encoder is kept and only the classifier head is
replaced, which is what makes this feasible on a few thousand images.

> **Not medical advice.** This is a student research artefact. It does not
> diagnose, and nothing it outputs should be shown to a user as a condition
> name. Malignant classes are removed from the data on purpose: a model that
> has never seen melanoma cannot imply anything about it, which is the honest
> position for a cosmetic tool.

**Trained classes (4):** `acne`, `hyperpigmentation`, `redness_rosacea`,
`dryness_eczema`

**Requested but NOT trained (2):** `uneven_tone`, `dark_circles` — no public
dataset labels either one. See the *Label gaps* section at the bottom for why,
and what to do instead. Inventing labels for them would produce a model that
scores well and means nothing.

**You edit one value:** `DATA_ROOT` in the Config cell.
""")

code(r"""
#@title 1 · Setup
!pip -q install "transformers>=4.44" onnx onnxruntime scikit-learn pandas pillow --upgrade 2>/dev/null

import json, math, os, random, re, time, warnings
from pathlib import Path
from collections import Counter, defaultdict

import numpy as np, pandas as pd, torch, torch.nn as nn
import transformers
from transformers import AutoImageProcessor, AutoModelForImageClassification
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.metrics import (classification_report, confusion_matrix, f1_score,
                             precision_recall_fscore_support, accuracy_score)

warnings.filterwarnings('ignore')
print('torch', torch.__version__, '| transformers', transformers.__version__,
      '| cuda', torch.cuda.is_available())
""")

code(r"""
#@title 2 · Config — the only cell you edit
from google.colab import drive
drive.mount('/content/drive')

DATA_ROOT = '/content/drive/MyDrive/skinsight/data'  #@param {type:"string"}
OUT_DIR   = '/content/drive/MyDrive/skinsight/output_multiclass'  #@param {type:"string"}

# Hugging Face backbones, fine-tuned on this data. Each is pretrained on
# ImageNet-21k or ImageNet-1k and has documented provenance and a permissive
# licence -- which is what lets the result be written into a register. An acne
# model from the Hub with undisclosed training data could not be.
#
# Both are trained on the SAME split and compared. ViT-base is the stronger
# model; DeiT-small is a third of the size and the realistic choice for a phone.
# Reporting both is more useful than asserting one.
BACKBONES = [
    'google/vit-base-patch16-224-in21k',      # Apache-2.0, 86M params
    'facebook/deit-small-patch16-224',        # Apache-2.0, 22M params
]
MODEL_ID, MODEL_VERSION = 'skin-conditions-hf', '0.1.0'

# 8 epochs each: two backbones on a free T4 is the budget, not the ideal. Raise
# it if the validation score is still climbing on the last epoch.
SEED, IMG_SIZE, BATCH_SIZE, EPOCHS, LR = 42, 224, 32, 8, 3e-5

# Multi-LABEL, not multi-class: one photograph can show acne and redness at the
# same time, and forcing a single winner would teach the model that it cannot.
CLASSES = ['acne', 'hyperpigmentation', 'redness_rosacea', 'dryness_eczema']

# Removed before training, never scored. Cancer is out of scope for a cosmetic
# tool, and a model trained on it would invite exactly the reading this project
# is built to avoid.
MALIGNANT = [
    'melanoma', 'basal cell carcinoma', 'squamous cell carcinoma',
    'merkel cell', 'kaposi', 'lentigo maligna', 'mycosis fungoides',
    'cutaneous lymphoma', 'angiosarcoma', 'dermatofibrosarcoma',
    'sebaceous carcinoma', 'malignant',
]

# Proposed deployment gate. These are a starting point, NOT a standard --
# agree them with your supervisor before quoting them as a requirement.
THRESHOLDS = {
    'macro_f1_min': 0.55,
    'worst_tone_accuracy_min': 0.70,
    'max_tone_accuracy_gap': 0.15,
}

random.seed(SEED); np.random.seed(SEED)
torch.manual_seed(SEED); torch.cuda.manual_seed_all(SEED)
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
print('backbones:', BACKBONES)
print('device:', DEVICE, '| classes:', CLASSES)
if DEVICE == 'cpu':
    print('WARNING: no GPU. Runtime -> Change runtime type -> T4 GPU.')
""")

md(r"""
## 3 · Ingest

Reads whichever of the supported datasets are present under `DATA_ROOT`. Each
has a different layout, so each gets its own reader; all of them produce the
same five columns — `path`, the four class flags, and `tone`.

`tone` is a Fitzpatrick bucket where the dataset provides one. **Only SCIN and
Fitzpatrick17k do.** ACNE04 has no skin-tone labels at all, so its rows carry
`tone = None` and are excluded from the fairness breakdown rather than being
guessed at.
""")

code(r"""
#@title 3 · Build one manifest from whatever is present
ROOT = Path(DATA_ROOT)
assert ROOT.exists(), f'DATA_ROOT does not exist: {ROOT}'

def blank(): return {c: 0 for c in CLASSES}

# Condition string -> our classes. Substring match, lowercase. A condition that
# matches nothing is dropped, which is deliberate: 114 dermatology labels do not
# collapse into four cosmetic buckets, and the ones that do not fit are not ours.
KEYWORDS = {
    'acne':            ['acne', 'comedone', 'pustular psoriasis of pregnancy'],
    'hyperpigmentation': ['melasma', 'hyperpigmentation', 'post-inflammatory',
                          'solar lentigo', 'lentigines', 'cafe au lait',
                          'becker nevus', 'nevus of ota'],
    'redness_rosacea': ['rosacea', 'erythema', 'perioral dermatitis',
                        'seborrheic dermatitis', 'telangiectasia', 'flushing'],
    'dryness_eczema':  ['eczema', 'atopic dermatitis', 'xerosis', 'ichthyosis',
                        'contact dermatitis', 'dyshidrotic', 'nummular'],
}

def classify(text):
    t = str(text).lower()
    if any(m in t for m in MALIGNANT):
        return None                      # dropped, counted below
    row = blank(); hit = False
    for cls, keys in KEYWORDS.items():
        if any(k in t for k in keys):
            row[cls] = 1; hit = True
    return row if hit else False         # False = no match, None = malignant

FITZ_BUCKET = {1: 'I-II', 2: 'I-II', 3: 'III-IV', 4: 'III-IV', 5: 'V-VI', 6: 'V-VI'}

records, stats = [], Counter()

# ---- ACNE04 : face photographs, filename carries the Hayashi grade ----------
for d in ROOT.rglob('*'):
    if not d.is_dir() or 'acne04' not in d.name.lower(): continue
    for p in list(d.rglob('*.jpg')) + list(d.rglob('*.png')):
        r = blank(); r['acne'] = 1
        records.append({'path': str(p), 'tone': None, 'src': 'acne04', **r})
    break

# ---- SCIN : CC BY 4.0, has dermatologist labels and skin-tone columns -------
for csv in list(ROOT.rglob('*scin*label*.csv')) + list(ROOT.rglob('*scin*cases*.csv')):
    df = pd.read_csv(csv, low_memory=False)
    cond_col = next((c for c in df.columns if 'dermatologist' in c.lower()
                     and 'skin_condition' in c.lower()), None)
    tone_col = next((c for c in df.columns if 'fitzpatrick' in c.lower()), None)
    path_col = next((c for c in df.columns if 'image' in c.lower() and
                     ('path' in c.lower() or 'id' in c.lower())), None)
    if not (cond_col and path_col): continue
    lookup = {p.name: p for p in ROOT.rglob('*.png')}
    lookup.update({p.name: p for p in ROOT.rglob('*.jpg')})
    for _, row in df.iterrows():
        res = classify(row[cond_col])
        if res is None: stats['dropped_malignant'] += 1; continue
        if res is False: stats['dropped_no_class'] += 1; continue
        name = Path(str(row[path_col])).name
        if name not in lookup: stats['missing_image'] += 1; continue
        tone = None
        if tone_col and pd.notna(row.get(tone_col)):
            m = re.search(r'[1-6]', str(row[tone_col]))
            tone = FITZ_BUCKET.get(int(m.group())) if m else None
        records.append({'path': str(lookup[name]), 'tone': tone, 'src': 'scin', **res})
    break

# ---- Fitzpatrick17k : CC BY-NC-SA 3.0, csv of labels + fitzpatrick_scale ----
for csv in ROOT.rglob('*fitzpatrick17k*.csv'):
    df = pd.read_csv(csv, low_memory=False)
    if 'label' not in df.columns: continue
    lookup = {p.stem: p for ext in ('*.jpg', '*.png') for p in ROOT.rglob(ext)}
    for _, row in df.iterrows():
        res = classify(row['label'])
        if res is None: stats['dropped_malignant'] += 1; continue
        if res is False: stats['dropped_no_class'] += 1; continue
        key = str(row.get('md5hash', ''))
        if key not in lookup: stats['missing_image'] += 1; continue
        scale = row.get('fitzpatrick_scale', -1)
        tone = FITZ_BUCKET.get(int(scale)) if str(scale).strip('-').isdigit() else None
        records.append({'path': str(lookup[key]), 'tone': tone,
                        'src': 'fitzpatrick17k', **res})
    break

data = pd.DataFrame(records).drop_duplicates('path').reset_index(drop=True)
assert len(data), (
    'No labelled images found. Expected one of: a folder named *acne04*, a '
    'SCIN labels csv, or fitzpatrick17k.csv -- somewhere under DATA_ROOT.')

print(f'images: {len(data)}   sources: {dict(Counter(data["src"]))}')
print(f'excluded -> malignant: {stats["dropped_malignant"]}, '
      f'no matching class: {stats["dropped_no_class"]}, '
      f'image file missing: {stats["missing_image"]}')
print('\npositives per class:')
for c in CLASSES:
    print(f'  {c:20} {int(data[c].sum()):5}  ({data[c].mean()*100:4.1f}%)')
print('\nskin tone coverage:')
print(data['tone'].fillna('unlabelled').value_counts().to_string())
""")

md(r"""
## 4 · Split

Stratified on the **rarest positive class present in each row**, so a class with
few examples is not concentrated in one split by chance.

The leakage check compares resolved file paths across splits. Clinical sets
often hold several photographs of one patient; without per-patient ids — which
none of these datasets publish — near-duplicates of the same lesion can still
land either side of the split. That is a real limitation, stated here rather
than hidden.
""")

code(r"""
#@title 4 · Stratified split, fixed seed, leakage check
freq = {c: data[c].sum() for c in CLASSES}
def strat_key(row):
    pos = [c for c in CLASSES if row[c] == 1]
    return min(pos, key=lambda c: freq[c]) if pos else 'none'

data['strat'] = data.apply(strat_key, axis=1)
counts = data['strat'].value_counts()
data['strat_safe'] = data['strat'].where(data['strat'].map(counts) >= 3, 'rare')

train_df, temp = train_test_split(data, test_size=0.30, random_state=SEED,
                                  stratify=data['strat_safe'])
val_df, test_df = train_test_split(temp, test_size=0.50, random_state=SEED,
                                   stratify=temp['strat_safe'])

overlap = (set(train_df.path) & set(val_df.path)) | (set(train_df.path) & set(test_df.path)) \
          | (set(val_df.path) & set(test_df.path))
assert not overlap, f'LEAKAGE: {len(overlap)} paths in more than one split'

for name, d in [('train', train_df), ('val', val_df), ('test', test_df)]:
    print(f'{name:6} {len(d):5}   ' + '  '.join(f'{c}={int(d[c].sum())}' for c in CLASSES))
print('\nno path appears in two splits.')

for name, d in [('train', train_df), ('val', val_df), ('test', test_df)]:
    d.to_csv(Path(OUT_DIR) / f'split_{name}.csv', index=False)
print('splits written to', OUT_DIR)
""")

code(r"""
#@title 5 · Data loading — normalisation read from each backbone's processor
from torchvision import transforms as T

def transforms_for(model_id):
    '''
    Each checkpoint was pretrained with its own normalisation. Hardcoding
    ImageNet constants quietly degrades any backbone that did not use them, so
    the numbers come from the processor the authors published alongside it.
    '''
    proc = AutoImageProcessor.from_pretrained(model_id)
    mean = tuple(getattr(proc, 'image_mean', (0.5, 0.5, 0.5)))
    std = tuple(getattr(proc, 'image_std', (0.5, 0.5, 0.5)))
    size = getattr(proc, 'size', None) or {}
    px = size.get('height') or size.get('shortest_edge') or IMG_SIZE
    train = T.Compose([
        T.Resize((px + 32, px + 32)),
        T.RandomResizedCrop(px, scale=(0.7, 1.0)),
        T.RandomHorizontalFlip(),
        T.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.10, hue=0.02),
        T.ToTensor(), T.Normalize(mean, std),
    ])
    evalt = T.Compose([T.Resize((px, px)), T.ToTensor(), T.Normalize(mean, std)])
    return train, evalt, px, mean, std

for mid in BACKBONES:
    _, _, px, mean, std = transforms_for(mid)
    print(f'{mid:42} {px}px  mean={tuple(round(v,3) for v in mean)}')

# Hue jitter is kept small on purpose. Colour IS the signal for pigmentation and
# redness, and a wide hue shift teaches the model to ignore the thing it is
# meant to detect -- and does it unevenly across skin tones.

class SkinSet(Dataset):
    def __init__(self, df, tf): self.df, self.tf = df.reset_index(drop=True), tf
    def __len__(self): return len(self.df)
    def __getitem__(self, i):
        r = self.df.iloc[i]
        img = Image.open(r['path']).convert('RGB')
        y = torch.tensor([r[c] for c in CLASSES], dtype=torch.float32)
        return self.tf(img), y

def loaders_for(model_id):
    train_tf, eval_tf, *_ = transforms_for(model_id)
    return {
        'train': DataLoader(SkinSet(train_df, train_tf), BATCH_SIZE, shuffle=True,
                            num_workers=2, pin_memory=True, drop_last=True),
        'val':   DataLoader(SkinSet(val_df, eval_tf), BATCH_SIZE, num_workers=2),
        'test':  DataLoader(SkinSet(test_df, eval_tf), BATCH_SIZE, num_workers=2),
    }
print('train/val/test:', len(train_df), len(val_df), len(test_df))
""")

code(r"""
#@title 6 · Fine-tune each Hugging Face backbone on the same split
# pos_weight = negatives/positives per class. Without it the model learns to
# answer "no" to every rare class and scores well doing it.
pos = train_df[CLASSES].sum().values.astype(float)
neg = len(train_df) - pos
pos_weight = torch.tensor(np.clip(neg / np.maximum(pos, 1), 1.0, 20.0),
                          dtype=torch.float32, device=DEVICE)
print('pos_weight:', dict(zip(CLASSES, pos_weight.cpu().numpy().round(2))))
criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)


def build(model_id):
    '''
    Transfer learning: keep the pretrained encoder, replace the classifier.

    `ignore_mismatched_sizes` is what permits that swap -- the checkpoint's head
    predicts 1k or 21k ImageNet classes and ours predicts 4. `problem_type`
    makes the config say multi-label, so anything reading the saved model later
    knows the outputs are independent sigmoids rather than a softmax.
    '''
    return AutoModelForImageClassification.from_pretrained(
        model_id,
        num_labels=len(CLASSES),
        problem_type='multi_label_classification',
        id2label={i: c for i, c in enumerate(CLASSES)},
        label2id={c: i for i, c in enumerate(CLASSES)},
        ignore_mismatched_sizes=True,
    ).to(DEVICE)


@torch.no_grad()
def collect(model, loader):
    model.eval(); P, Y = [], []
    for x, y in loader:
        with torch.amp.autocast('cuda', enabled=(DEVICE == 'cuda')):
            logits = model(pixel_values=x.to(DEVICE)).logits
        P.append(torch.sigmoid(logits).float().cpu()); Y.append(y)
    return torch.cat(P).numpy(), torch.cat(Y).numpy()


def finetune(model_id):
    dl = loaders_for(model_id)
    model = build(model_id)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    scaler = torch.amp.GradScaler('cuda', enabled=(DEVICE == 'cuda'))

    best, best_state = -1.0, None
    for ep in range(1, EPOCHS + 1):
        model.train(); run = 0.0
        for x, y in dl['train']:
            x, y = x.to(DEVICE, non_blocking=True), y.to(DEVICE, non_blocking=True)
            opt.zero_grad(set_to_none=True)
            with torch.amp.autocast('cuda', enabled=(DEVICE == 'cuda')):
                loss = criterion(model(pixel_values=x).logits, y)
            scaler.scale(loss).backward(); scaler.step(opt); scaler.update()
            run += loss.item()
        sched.step()
        p, yv = collect(model, dl['val'])
        f1 = f1_score(yv, (p >= 0.5).astype(int), average='macro', zero_division=0)
        print(f'  epoch {ep:2}/{EPOCHS}  loss {run/len(dl["train"]):.4f}  val macro-F1 {f1:.4f}')
        if f1 > best:
            best = f1
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state)
    n_params = sum(p.numel() for p in model.parameters())
    return model, dl, float(best), n_params


trained = {}
for mid in BACKBONES:
    print(f'\n=== {mid} ===')
    t0 = time.time()
    model, dl, val_f1, n_params = finetune(mid)
    trained[mid] = {'model': model, 'loaders': dl, 'val_f1': val_f1,
                    'params': n_params, 'train_min': (time.time() - t0) / 60}
    print(f'  best val macro-F1 {val_f1:.4f} | {n_params/1e6:.1f}M params '
          f'| {trained[mid]["train_min"]:.1f} min')

# The winner is chosen on VALIDATION. Choosing it on test would make the test
# score a report of the choice rather than an estimate of performance.
BEST_ID = max(trained, key=lambda k: trained[k]['val_f1'])
model, loaders = trained[BEST_ID]['model'], trained[BEST_ID]['loaders']
torch.save(trained[BEST_ID]['model'].state_dict(), Path(OUT_DIR) / 'model.pt')
print(f'\nselected on validation: {BEST_ID}')
""")

code(r"""
#@title 7 · Per-class thresholds, tuned on validation only
# 0.5 is arbitrary for an imbalanced multi-label problem. Each class gets the
# threshold that maximises its F1 on VALIDATION. Tuning on test would make the
# test score a description of the tuning, not of the model.
pv, yv = collect(model, loaders['val'])
THRESH = {}
for i, c in enumerate(CLASSES):
    grid = np.arange(0.05, 0.96, 0.01)
    scores = [f1_score(yv[:, i], (pv[:, i] >= t).astype(int), zero_division=0) for t in grid]
    THRESH[c] = float(grid[int(np.argmax(scores))])
print('thresholds:', {k: round(v, 2) for k, v in THRESH.items()})
""")

code(r"""
#@title 8 · Evaluate the selected model on the held-out test split
pt, yt = collect(model, loaders['test'])
pred = np.zeros_like(yt, dtype=int)
for i, c in enumerate(CLASSES):
    pred[:, i] = (pt[:, i] >= THRESH[c]).astype(int)

macro_f1 = f1_score(yt, pred, average='macro', zero_division=0)
micro_f1 = f1_score(yt, pred, average='micro', zero_division=0)
prec, rec, f1s, sup = precision_recall_fscore_support(yt, pred, zero_division=0)

print(f'macro-F1 {macro_f1:.4f}   micro-F1 {micro_f1:.4f}   n={len(yt)}\n')
print(f'{"class":22}{"prec":>7}{"recall":>8}{"F1":>7}{"support":>9}')
for i, c in enumerate(CLASSES):
    print(f'{c:22}{prec[i]:7.3f}{rec[i]:8.3f}{f1s[i]:7.3f}{int(sup[i]):9}')

print('\nconfusion matrix per class  [[TN FP] [FN TP]]')
for i, c in enumerate(CLASSES):
    cm = confusion_matrix(yt[:, i], pred[:, i], labels=[0, 1])
    print(f'  {c:22} {cm.tolist()}')

per_class_recall = {c: float(rec[i]) for i, c in enumerate(CLASSES)}
""")

code(r"""
#@title 8b · Head-to-head: every backbone on the SAME test split
# Each model is scored with thresholds tuned on its OWN validation predictions.
# Reusing the selected model's thresholds would hand it an advantage that has
# nothing to do with the backbone.
comparison = {}
for mid, t in trained.items():
    pv_m, yv_m = collect(t['model'], t['loaders']['val'])
    th = []
    for i in range(len(CLASSES)):
        grid = np.arange(0.05, 0.96, 0.01)
        th.append(float(grid[int(np.argmax(
            [f1_score(yv_m[:, i], (pv_m[:, i] >= g).astype(int), zero_division=0)
             for g in grid]))]))
    pt_m, yt_m = collect(t['model'], t['loaders']['test'])
    pred_m = np.stack([(pt_m[:, i] >= th[i]).astype(int) for i in range(len(CLASSES))], 1)
    _, rec_m, f1_m, _ = precision_recall_fscore_support(yt_m, pred_m, zero_division=0)
    comparison[mid] = {
        'params_m': round(t['params'] / 1e6, 1),
        'train_min': round(t['train_min'], 1),
        'val_macro_f1': round(t['val_f1'], 4),
        'test_macro_f1': round(float(f1_score(yt_m, pred_m, average='macro', zero_division=0)), 4),
        'per_class_recall': {c: round(float(rec_m[i]), 3) for i, c in enumerate(CLASSES)},
    }

print(f'{"backbone":44}{"params":>8}{"min":>6}{"val F1":>9}{"test F1":>9}')
for mid, r in comparison.items():
    mark = ' <- selected' if mid == BEST_ID else ''
    print(f'{mid:44}{r["params_m"]:7.1f}M{r["train_min"]:6.1f}'
          f'{r["val_macro_f1"]:9.4f}{r["test_macro_f1"]:9.4f}{mark}')

print('\nRead this fairly: both were fine-tuned for the same few epochs on the')
print('same data. A gap here is a result for THIS budget, not a general claim')
print('about the architectures.')
""")

code(r"""
#@title 9 · Fairness — per skin tone (only where the dataset labelled it)
tones = test_df['tone'].values
labelled = np.array([t is not None and not (isinstance(t, float) and math.isnan(t))
                     for t in tones])

tone_report, worst_acc, gap = {}, None, None
if labelled.sum() == 0:
    print('No skin-tone labels in the test split — fairness cannot be reported.')
    print('That is a gap, not a pass. NFR-SAFE-008 is unmet until it is filled.')
else:
    exact = (pred == yt).all(axis=1)          # all four labels right
    print(f'{"tone":10}{"n":>6}{"exact-match acc":>18}{"macro-F1":>11}')
    for t in sorted({x for x in tones[labelled]}):
        m = np.array([x == t for x in tones])
        if m.sum() < 10:
            print(f'{t:10}{m.sum():6}   too few to report'); continue
        acc = float(exact[m].mean())
        f1t = f1_score(yt[m], pred[m], average='macro', zero_division=0)
        tone_report[t] = {'n': int(m.sum()), 'accuracy': acc, 'macro_f1': float(f1t)}
        print(f'{t:10}{m.sum():6}{acc:18.3f}{f1t:11.3f}')
    if tone_report:
        accs = [v['accuracy'] for v in tone_report.values()]
        worst_acc, gap = float(min(accs)), float(max(accs) - min(accs))
        print(f'\nworst group {worst_acc:.3f}   spread {gap:.3f}')
        print('Groups are the dataset\'s own Fitzpatrick labels, not estimates.')
""")

code(r"""
#@title 10 · Export to ONNX, check parity, time it on CPU
onnx_path = str(Path(OUT_DIR) / f'{MODEL_ID}-{MODEL_VERSION}.onnx')
model.eval().cpu()


class OnnxWrapper(nn.Module):
    '''
    A transformers model takes `pixel_values` and returns a ModelOutput object.
    ONNX needs a plain tensor in and a plain tensor out, so the wrapper unwraps
    `.logits`. Sigmoid stays OUT of the graph: the per-class thresholds are
    applied by the caller, and burying an activation in the export is how a
    served model ends up double-applying it.
    '''
    def __init__(self, m): super().__init__(); self.m = m
    def forward(self, image): return self.m(pixel_values=image).logits


_, _, PX, _, _ = transforms_for(BEST_ID)
wrapper = OnnxWrapper(model).eval()
dummy = torch.randn(1, 3, PX, PX)
torch.onnx.export(wrapper, dummy, onnx_path, input_names=['image'],
                  output_names=['logits'], opset_version=17,
                  dynamic_axes={'image': {0: 'batch'}, 'logits': {0: 'batch'}})

import onnxruntime as ort
sess = ort.InferenceSession(onnx_path, providers=['CPUExecutionProvider'])
with torch.no_grad():
    torch_out = wrapper(dummy).numpy()
onnx_out = sess.run(None, {'image': dummy.numpy()})[0]
max_diff = float(np.abs(torch_out - onnx_out).max())

for _ in range(3): sess.run(None, {'image': dummy.numpy()})
t0 = time.time()
for _ in range(20): sess.run(None, {'image': dummy.numpy()})
cpu_ms = (time.time() - t0) / 20 * 1000

size_mb = os.path.getsize(onnx_path) / 1e6
print(f'exported: {BEST_ID}')
print(f'onnx {size_mb:.1f} MB | input {PX}x{PX} | torch-vs-onnx max diff {max_diff:.2e} '
      f'| CPU {cpu_ms:.1f} ms/image')
if size_mb > 100:
    print('NOTE: over 100 MB. Large for a phone -- the smaller backbone in the')
    print('comparison table may be the better deployment choice even if its F1 is lower.')
model.to(DEVICE)
""")

code(r"""
#@title 11 · Verdict — paste this whole block back
checks = {
    'macro_f1': (macro_f1, THRESHOLDS['macro_f1_min'], macro_f1 >= THRESHOLDS['macro_f1_min']),
    'worst_tone_accuracy': (worst_acc, THRESHOLDS['worst_tone_accuracy_min'],
                            worst_acc is not None and worst_acc >= THRESHOLDS['worst_tone_accuracy_min']),
    'tone_gap': (gap, THRESHOLDS['max_tone_accuracy_gap'],
                 gap is not None and gap <= THRESHOLDS['max_tone_accuracy_gap']),
    'onnx_parity': (max_diff, 1e-4, max_diff < 1e-4),
}
passed = all(ok for _, _, ok in checks.values())

print('DEPLOYMENT GATE')
for k, (got, want, ok) in checks.items():
    shown = 'not measurable' if got is None else f'{got:.4f}'
    print(f'  {"PASS" if ok else "FAIL"}  {k:22} got {shown:>14}  threshold {want}')
print('\nVERDICT:', 'PASSES the proposed gate' if passed else 'DOES NOT pass — do not deploy')
if gap is None:
    print('NOTE: no skin-tone labels in the test set, so two of the four checks could')
    print('not be evaluated. Untested is not the same as passed.')

entry = {
    'modelId': MODEL_ID, 'modelVersion': MODEL_VERSION,
    'backbone': BEST_ID, 'backbonesCompared': comparison,
    'selectedOn': 'validation macro-F1',
    'task': 'multi-label facial skin condition', 'classes': CLASSES,
    'notTrained': ['uneven_tone', 'dark_circles'],
    'trainedOn': sorted(set(train_df['src'])),
    'counts': {'train': len(train_df), 'val': len(val_df), 'test': len(test_df)},
    'thresholds': {k: round(v, 3) for k, v in THRESH.items()},
    'metrics': {
        'macro_f1': round(float(macro_f1), 4),
        'micro_f1': round(float(micro_f1), 4),
        'per_class_recall': {k: round(v, 4) for k, v in per_class_recall.items()},
        'per_tone': tone_report,
        'worst_tone_accuracy': worst_acc, 'tone_gap': gap,
    },
    'onnx': {'sizeMb': round(size_mb, 2), 'cpuMsPerImage': round(cpu_ms, 1),
             'maxAbsDiffVsTorch': max_diff},
    'excluded': {'malignant_rows_dropped': stats['dropped_malignant']},
    'gate': {k: {'value': v[0], 'threshold': v[1], 'pass': bool(v[2])} for k, v in checks.items()},
    'verdict': 'PASS' if passed else 'FAIL',
    'seed': SEED,
}
Path(OUT_DIR, 'appendix_entry.json').write_text(json.dumps(entry, indent=2))
print('\n' + '=' * 62); print(json.dumps(entry, indent=2)); print('=' * 62)
""")

md(r"""
## Label gaps — the two classes this does not train

**`uneven_tone`** — no public dataset labels it. It is a cosmetic judgement
about variation across a face, not a dermatological diagnosis, so it does not
appear in clinical atlases at all.
*Mitigation:* measure it, do not classify it. Compute ITA (Individual Typology
Angle) across sampled face regions and report the spread. That is a defined,
reproducible number from the image, and it needs no labels — unlike a learned
class trained on labels nobody has published.

**`dark_circles`** — same absence, and periorbital hyperpigmentation is barely
represented in clinical sets.
*Mitigation:* a region comparison — mean ITA under the eye versus the cheek on
the same face. It needs face landmarks but no training data, and it degrades
honestly: no face detected, no measurement, rather than a confident guess.

Training either as a class would mean inventing labels. The model would then
score well against its own invention and tell you nothing.

## Honest limitations

1. **Domain shift.** SCIN and Fitzpatrick17k are clinical photographs of skin
   anywhere on the body. The deployment target is a front-facing phone selfie.
   Test numbers from these datasets will overstate real performance.
2. **No patient ids.** None of these datasets publish them, so several images of
   one person can straddle the split. Scores may be optimistic.
3. **Fitzpatrick17k is CC BY-NC-SA 3.0.** Non-commercial, and share-alike — if
   you train on it, check what the licence implies for the weights before any
   commercial use. SCIN (CC BY 4.0) carries no such restriction.
4. **The gate numbers are a proposal**, not a standard. Agree them with your
   supervisor before citing them as a requirement.
5. **Nothing here is validated clinically.** It is a research artefact, and a
   failing result is a legitimate, reportable finding.
""")

nb = {
    "cells": [
        {"cell_type": k, "metadata": {},
         **({"source": s.splitlines(keepends=True)} if k == "markdown" else
            {"source": s.splitlines(keepends=True), "outputs": [], "execution_count": None})}
        for k, s in C
    ],
    "metadata": {
        "accelerator": "GPU",
        "colab": {"provenance": [], "gpuType": "T4"},
        "kernelspec": {"display_name": "Python 3", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4, "nbformat_minor": 0,
}

out = sys.argv[1]
io.open(out, "w", encoding="utf-8").write(json.dumps(nb, indent=1, ensure_ascii=False))
print(f"wrote {out}: {len(C)} cells "
      f"({sum(1 for k,_ in C if k=='code')} code, {sum(1 for k,_ in C if k=='markdown')} markdown)")
