"""
moderation.py
==============
Moduli ya kutambua maudhui ya uchi (NSFW) kwenye picha na video
kabla ya kuchapishwa hadharani (public feed).

Inatumia NudeNet (https://github.com/notAI-tech/NudeNet) - model ya
bure/open-source, haihitaji API key wala malipo. Model inapakuliwa
moja kwa moja (mara ya kwanza tu) inapotumika kwa mara ya kwanza,
hivyo unahitaji internet wakati wa kuisakinisha/kuitumia mara ya
kwanza kwenye server yako.

Sakinisha:
    pip install nudenet opencv-python-headless

Jinsi inavyofanya kazi:
- PICHA: NudeDetector inachambua picha moja kwa moja, inarudisha
  "detections" (sehemu za mwili zilizogunduliwa + confidence score).
- VIDEO: Tunachukua "frames" kadhaa (mfano: kila baada ya sekunde N,
  au idadi maalum ya frames zilizosambaa sawasawa kwenye video nzima),
  kila frame inachambuliwa kama picha, na tunachukua score kubwa zaidi
  kati ya frames zote.

Uamuzi (decision):
- score >= REJECT_THRESHOLD      -> "rejected"       (haionekani public,
                                                        warning kwa user)
- REVIEW_THRESHOLD <= score < REJECT_THRESHOLD -> "manual_review"
                                                        (haionekani public
                                                        mpaka admin aangalie)
- score < REVIEW_THRESHOLD       -> "approved"       (inaendelea kama kawaida)

Unaweza kubadilisha thresholds hapa chini kulingana na jinsi
unavyotaka mfumo uwe mkali au mpole.
"""

import os
import tempfile

# ==================== SETTINGS (badilisha kama unahitaji) ====================

# Chini ya hii = salama kabisa
REVIEW_THRESHOLD = 0.45

# Zaidi/sawa na hii = ukiukaji wa wazi -> rejected moja kwa moja
REJECT_THRESHOLD = 0.75

# Sehemu za mwili ambazo, zikigundulika, zinahesabiwa kama ukiukaji.
# (Majina haya yanatoka NudeNet v3 default detector labels)
UNSAFE_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "ANUS_COVERED",          # mara nyingi bado ni "suggestive" -> tutaipa uzito kidogo
    "FEMALE_BREAST_COVERED", # suggestive tu, si ukiukaji mkubwa
    "MALE_BREAST_EXPOSED",
}

# Labels ambazo ni "wazi kabisa" (weight kamili ya score yake)
HARD_UNSAFE_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}

# Idadi ya frames za kuchambua kwenye video (zaidi = sahihi zaidi lakini
# polepole zaidi)
VIDEO_FRAMES_TO_SAMPLE = 6

_detector = None


def _get_detector():
    """Load NudeDetector mara moja tu (singleton) - model ni nzito kuipakia."""
    global _detector
    if _detector is None:
        from nudenet import NudeDetector
        _detector = NudeDetector()
    return _detector


def _score_from_detections(detections):
    """
    Chukua detections (list ya dict zenye 'class' na 'score') na
    kokotoa score moja ya juu zaidi ya 'ukiukaji', pamoja na majina
    ya labels zilizogundulika.
    """
    best_score = 0.0
    matched_labels = []

    for det in detections or []:
        label = det.get("class") or det.get("label")
        score = float(det.get("score", 0.0))

        if label in HARD_UNSAFE_LABELS:
            weight = 1.0
        elif label in UNSAFE_LABELS:
            weight = 0.6  # suggestive/covered -> tunapunguza uzito
        else:
            continue

        weighted = score * weight
        if weighted > best_score:
            best_score = weighted
        matched_labels.append(f"{label}:{round(score, 2)}")

    return best_score, matched_labels


def analyze_image(image_path):
    """
    Chambua picha moja. Rudisha (score: float 0..1, labels: list[str]).
    """
    try:
        detector = _get_detector()
        detections = detector.detect(image_path)
        return _score_from_detections(detections)
    except Exception as e:
        print("[moderation] analyze_image error:", e)
        # Ikiwa detector imeshindwa kabisa kufanya kazi, ni salama zaidi
        # kutuma kwenye manual_review badala ya kuruhusu moja kwa moja
        # au kukataa moja kwa moja.
        return -1.0, ["ERROR:" + str(e)]


def analyze_video(video_path, num_frames=VIDEO_FRAMES_TO_SAMPLE):
    """
    Chukua frames kadhaa kutoka video na uzichambue kama picha.
    Rudisha (score ya juu zaidi kati ya frames zote, labels zilizogundulika).
    """
    try:
        import cv2
    except ImportError as e:
        print("[moderation] opencv haijasakinishwa:", e)
        return -1.0, ["ERROR: opencv-python-headless haijasakinishwa"]

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return -1.0, ["ERROR: video haikuweza kufunguliwa"]

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_frames <= 0:
        cap.release()
        return -1.0, ["ERROR: video haina frames"]

    # Chagua frame indices zilizosambaa sawasawa kwenye video nzima
    # (tunaepuka frame ya kwanza kabisa na ya mwisho kabisa mara nyingi
    # ni nyeusi/intro)
    step = max(total_frames // (num_frames + 1), 1)
    frame_indices = [step * (i + 1) for i in range(num_frames)]
    frame_indices = [i for i in frame_indices if i < total_frames]

    best_score = 0.0
    all_labels = []

    tmp_dir = tempfile.mkdtemp(prefix="nsfw_frames_")
    try:
        for idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            success, frame = cap.read()
            if not success:
                continue

            frame_path = os.path.join(tmp_dir, f"frame_{idx}.jpg")
            cv2.imwrite(frame_path, frame)

            score, labels = analyze_image(frame_path)
            if score < 0:
                # error kwenye frame hii - endelea na nyingine
                continue
            if score > best_score:
                best_score = score
            all_labels.extend(labels)

            try:
                os.remove(frame_path)
            except OSError:
                pass

            # Kama tumeshapata ukiukaji wa wazi kabisa, hakuna sababu
            # ya kuendelea kuchambua frames zilizobaki (inaokoa muda)
            if best_score >= REJECT_THRESHOLD:
                break
    finally:
        cap.release()
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    return best_score, all_labels


def moderate_media(file_path, media_type):
    """
    Kazi kuu itakayoitwa na app.py.

    MODERATION IMEZIMWA (DISABLED) — post zote zinaidhinishwa moja kwa moja.
    Ili kuwasha tena, ondoa return hapa chini na rudisha logic ya NudeNet.

    Parameters
    ----------
    file_path : absolute path ya faili lililohifadhiwa serverini
    media_type : 'image' au 'video'

    Returns
    -------
    dict yenye:
        decision : 'approved' | 'rejected' | 'manual_review'
        score    : float (0..1, au -1 kama detector imeshindwa)
        labels   : list ya majina ya sehemu zilizogundulika (kwa admin)
    """
    # ===== MODERATION DISABLED =====
    return {"decision": "approved", "score": 0.0, "labels": []}

    if not file_path or not os.path.isfile(file_path):
        return {"decision": "approved", "score": 0.0, "labels": []}

    if media_type == "image":
        score, labels = analyze_image(file_path)
    elif media_type == "video":
        score, labels = analyze_video(file_path)
    else:
        return {"decision": "approved", "score": 0.0, "labels": []}

    if score < 0:
        # Detector imeshindwa kufanya kazi (mfano: model haipo, faili
        # limeharibika). Salama zaidi ni kutuma kwa admin kuliko
        # kuruhusu au kukataa moja kwa moja bila uhakika.
        return {"decision": "manual_review", "score": score, "labels": labels}

    if score >= REJECT_THRESHOLD:
        decision = "rejected"
    elif score >= REVIEW_THRESHOLD:
        decision = "manual_review"
    else:
        decision = "approved"

    return {"decision": decision, "score": round(score, 4), "labels": labels}
