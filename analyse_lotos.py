import os
import re
import json
import hashlib
import logging
import unicodedata
from io import BytesIO
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, ImageEnhance
import pytesseract


# ==========================================
# CONFIGURATION LOTOAGENDA
# ==========================================

SITE = "https://www.lotoagenda.ch/"
FICHIER_SORTIE = "docs/lotos.json"

MOIS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12
}

CANTONS = {
    "fribourg": "FR",
    "neuchatel": "NE",
    "vaud": "VD",
    "jura": "JU",
    "valais": "VS",
    "geneve": "GE",
    "berne": "BE"
}

LOCALITES = {
    "FR": [
        "Fribourg", "Bulle", "Romont",
        "Estavayer", "Chatonnaye",
        "Ecuvillens", "Marly", "Dudingen",
        "Villars-sur-Glane", "Attalens",
        "Payerne"
    ],
    "NE": [
        "Neuchatel", "La Chaux-de-Fonds",
        "Le Locle", "Cernier", "Valangin",
        "Colombier", "Boudry", "Fleurier"
    ],
    "VD": [
        "Lausanne", "Yverdon", "Morges",
        "Nyon", "Aigle", "Montreux",
        "Vevey", "Orbe"
    ],
    "JU": [
        "Delemont", "Porrentruy",
        "Saignelegier"
    ],
    "VS": [
        "Sion", "Martigny", "Monthey",
        "Sierre"
    ],
    "GE": [
        "Geneve", "Carouge", "Meyrin"
    ],
    "BE": [
        "Bienne", "Saint-Imier",
        "Tramelan", "Tavannes", "Moutier"
    ]
}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s - %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 LotoagendaCalendrier/1.0"
})


# ==========================================
# OUTILS
# ==========================================

def normaliser(texte):
    texte = texte.lower()
    texte = unicodedata.normalize("NFD", texte)
    return "".join(
        c for c in texte
        if unicodedata.category(c) != "Mn"
    )


def date_valide(jour, mois, annee):
    try:
        d = date(int(annee), int(mois), int(jour))

        # Ignorer les dates manifestement anciennes
        if d.year < 2026:
            return None

        return d.isoformat()

    except ValueError:
        return None


# ==========================================
# RECUPERATION DES AFFICHES WEBADOR
# ==========================================

def recuperer_images():

    response = session.get(SITE, timeout=40)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    images = []
    deja_vues = set()

    for img in soup.find_all("img"):

        candidats = []

        for attribut in [
            "src",
            "data-src",
            "data-original",
            "data-lazy-src"
        ]:
            valeur = img.get(attribut)
            if valeur:
                candidats.append(valeur)

        for attribut in ["srcset", "data-srcset"]:
            valeur = img.get(attribut, "")

            if valeur:
                for partie in valeur.split(","):
                    url = partie.strip().split(" ")[0]
                    candidats.append(url)

        for url in candidats:

            url = urljoin(SITE, url)

            if "primary.jwwb.nl" not in url:
                continue

            # Ne garder qu'une adresse par image.
            identifiant = urlparse(url).path

            if identifiant in deja_vues:
                continue

            deja_vues.add(identifiant)
            images.append(url)

    logging.info(
        "%s images Webador detectees",
        len(images)
    )

    return images


# ==========================================
# LECTURE DES IMAGES
# ==========================================

def lire_affiche(url):

    response = session.get(url, timeout=40)
    response.raise_for_status()

    image = Image.open(BytesIO(response.content))
    image = ImageOps.exif_transpose(image)

    largeur, hauteur = image.size

    # Ecarter les petites icones.
    if largeur < 350 or hauteur < 250:
        return None

    image = image.convert("RGB")

    # Agrandir les images peu lisibles.
    if largeur < 1200:
        facteur = 1200 / largeur

        image = image.resize((
            int(largeur * facteur),
            int(hauteur * facteur)
        ))

    gris = ImageOps.grayscale(image)
    gris = ImageEnhance.Contrast(gris).enhance(1.7)

    texte1 = pytesseract.image_to_string(
        image,
        lang="fra",
        config="--psm 11"
    )

    texte2 = pytesseract.image_to_string(
        gris,
        lang="fra",
        config="--psm 11"
    )

    return texte1 + "\n" + texte2


# ==========================================
# RECONNAISSANCE DES DATES
# ==========================================

def trouver_date(texte):

    texte = normaliser(texte)

    # Exemple : 15.11.2026 ou 15/11/2026
    motif_numerique = (
        r"\b(\d{1,2})[./-]"
        r"(\d{1,2})[./-]"
        r"(20\d{2})\b"
    )

    for resultat in re.finditer(
        motif_numerique, texte
    ):

        jour, mois, annee = resultat.groups()

        valeur = date_valide(jour, mois, annee)

        if valeur:
            return valeur

    # Exemple : dimanche 15 novembre 2026
    motif_texte = (
        r"\b(\d{1,2})\s+"
        r"([a-z]+)\s+"
        r"(20\d{2})\b"
    )

    for resultat in re.finditer(
        motif_texte, texte
    ):

        jour, nom_mois, annee = resultat.groups()

        if nom_mois not in MOIS:
            continue

        valeur = date_valide(
            jour,
            MOIS[nom_mois],
            annee
        )

        if valeur:
            return valeur

    return None


# ==========================================
# RECONNAISSANCE DE L'HEURE
# ==========================================

def trouver_heure(texte):

    texte = normaliser(texte)

    motif = r"\b([01]?\d|2[0-3])\s*[h:.]\s*([0-5]\d)\b"

    resultat = re.search(motif, texte)

    if resultat:
        heures = int(resultat.group(1))
        minutes = int(resultat.group(2))

        return f"{heures:02d}:{minutes:02d}"

    return ""


# ==========================================
# RECONNAISSANCE LOCALITE / CANTON
# ==========================================

def trouver_localite(texte):

    texte = normaliser(texte)

    for canton, villes in LOCALITES.items():

        for ville in villes:

            if normaliser(ville) in texte:
                return ville, canton

    for nom, canton in CANTONS.items():

        if nom in texte:
            return nom.capitalize(), canton

    return "", ""


# ==========================================
# SAUVEGARDE
# ==========================================

def sauvegarder(lotos, a_verifier):

    os.makedirs("docs", exist_ok=True)

    lotos.sort(
        key=lambda x: (
            x["date"],
            x.get("heure", ""),
            x.get("ville", "")
        )
    )

    with open(
        FICHIER_SORTIE,
        "w",
        encoding="utf-8"
    ) as fichier:

        json.dump(
            lotos,
            fichier,
            ensure_ascii=False,
            indent=2
        )

    # Les affiches dont les informations
    # sont incertaines restent consultables.

    with open(
        "docs/a_verifier.json",
        "w",
        encoding="utf-8"
    ) as fichier:

        json.dump(
            a_verifier,
            fichier,
            ensure_ascii=False,
            indent=2
        )

    logging.info(
        "%s lotos enregistres",
        len(lotos)
    )

    logging.info(
        "%s affiches a verifier",
        len(a_verifier)
    )


# ==========================================
# PROGRAMME PRINCIPAL
# ==========================================

def main():

    logging.info("Debut analyse lotoagenda.ch")

    images = recuperer_images()

    if not images:
        raise RuntimeError(
            "Aucune image trouvee. "
            "Les donnees existantes sont conservees."
        )

    lotos = []
    a_verifier = []
    deja_traites = set()
    lectures_reussies = 0

    for numero, url in enumerate(images, 1):

        logging.info(
            "Analyse image %s / %s",
            numero,
            len(images)
        )

        identifiant = hashlib.sha256(
            urlparse(url).path.encode()
        ).hexdigest()[:16]

        if identifiant in deja_traites:
            continue

        deja_traites.add(identifiant)

        try:

            texte = lire_affiche(url)

            if not texte:
                continue

            lectures_reussies += 1

            # Ignorer les images sans indication
            # de loto pour limiter les erreurs.
            texte_normalise = normaliser(texte)

            if "loto" not in texte_normalise:
                continue

            jour = trouver_date(texte)
            heure = trouver_heure(texte)
            ville, canton = trouver_localite(texte)

            if not jour or not canton:

                a_verifier.append({
                    "id": identifiant,
                    "image": url,
                    "date": jour or "",
                    "ville": ville,
                    "canton": canton,
                    "heure": heure,
                    "raison": "Date ou canton non reconnu"
                })

                continue

            loto = {
                "id": identifiant,
                "date": jour,
                "ville": ville,
                "canton": canton,
                "heure": heure,
                "image": url
            }

            lotos.append(loto)

            logging.info(
                "Loto trouve : %s - %s (%s)",
                jour,
                ville,
                canton
            )

        except Exception as erreur:

            logging.warning(
                "Erreur image %s : %s",
                numero,
                erreur
            )

            a_verifier.append({
                "id": identifiant,
                "image": url,
                "raison": str(erreur)
            })

    if lectures_reussies == 0:
        raise RuntimeError(
            "Aucune image lisible. "
            "Les anciennes donnees sont conservees."
        )

    if not lotos and os.path.exists(FICHIER_SORTIE):
        logging.warning(
            "Aucun loto reconnu. "
            "Le calendrier existant est conserve."
        )
        return

    sauvegarder(lotos, a_verifier)

    logging.info("Analyse terminee")


if __name__ == "__main__":
    main()
