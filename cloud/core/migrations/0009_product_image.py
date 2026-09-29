from django.db import migrations, models


def default_image(name, category=""):
    text = f"{name} {category}".casefold()
    groups = {
        "beverages": ("bebida", "água", "agua", "refrigerante", "suco", "cerveja", "vinho", "café", "cafe", "leite"),
        "bakery": ("pão", "pao", "bolo", "biscoito", "padaria", "massa"),
        "cleaning": ("limpeza", "sabão", "sabao", "detergente", "desinfetante", "higiene", "papel"),
        "produce": ("fruta", "verdura", "legume", "hortifruti", "banana", "maçã", "maca", "tomate"),
    }
    key = next((key for key, words in groups.items() if any(word in text for word in words)), "grocery")
    return f"/assets/product-images/{key}.svg"


def backfill_images(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    for product in Product.objects.select_related("category").filter(image_url=""):
        product.image_url = default_image(product.name, product.category.name if product.category else "")
        product.save(update_fields=["image_url"])


class Migration(migrations.Migration):
    dependencies = [("core", "0008_stockreceipt_source_file")]

    operations = [
        migrations.AddField(
            model_name="product",
            name="image_url",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.RunPython(backfill_images, migrations.RunPython.noop),
    ]
