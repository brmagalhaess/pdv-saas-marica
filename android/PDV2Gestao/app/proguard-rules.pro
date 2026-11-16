# v1.0 sai sem minificação (isMinifyEnabled = false).
# Se o R8 for ligado no futuro, manter os serializadores do kotlinx.serialization:
-keepattributes *Annotation*, InnerClasses
-keepclassmembers class br.com.crowntech.pdv2gestao.core.model.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
