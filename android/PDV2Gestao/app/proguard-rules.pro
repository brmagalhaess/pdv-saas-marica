# R8 ligado no release. kotlinx.serialization e Ktor trazem regras próprias;
# estas cobrem os modelos do app e avisos de classes opcionais do Ktor.
-keepattributes *Annotation*, InnerClasses, Signature, EnclosingMethod
-keepclassmembers class br.com.crowntech.pdv2gestao.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class br.com.crowntech.pdv2gestao.**$$serializer { *; }
-dontwarn org.slf4j.**
-dontwarn java.lang.management.**
-dontwarn io.ktor.util.debug.**
