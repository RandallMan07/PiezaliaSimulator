# Prueba técnica: el catálogo de Desguaces Rovira en Piezalia

## La situación

Portal Hero conecta a desguaces, concesionarios de motos y tiendas con los marketplaces donde venden: publicamos y mantenemos su catálogo, les traemos los pedidos y gestionamos las conversaciones con sus compradores (chat, WhatsApp, CRM). Esta prueba toca una de esas piezas, la sincronización del catálogo, con un cliente del eje de desguaces.

Desguaces Rovira, en Castellón, acaba de firmar con nosotros. Jordi, el dueño, vende en Piezalia, un marketplace de piezas de coche, con cuatro cuentas distintas (una por nave). Hasta ahora las llevaba una integración anterior que ya no existe, así que las cuentas tienen anuncios de antes de llegar a nosotros.

Su programa de gestión exporta varias veces al día el catálogo completo en un CSV, al que llamamos «feed». Nuestro trabajo es que lo que hay en Piezalia sea lo que dice el feed: cada pieza, en las cuentas donde Jordi la quiere, con su título y su precio.

En esta prueba construyes una versión pequeña de ese servicio contra un simulador de Piezalia que se comporta como el portal de verdad.

## Lo que sabemos del negocio

1. El feed es el catálogo completo. Una pieza que no viene ya no está a la venta y hay que retirarla de Piezalia cuanto antes: el mes pasado Rovira vendió por Piezalia un motor que ya había vendido en el mostrador, y acabó en reclamación del comprador y penalización del portal. Eso sí: el programa de gestión de Rovira es viejo y alguna vez ha exportado el fichero a medias.
2. Retirar un anuncio por error también es caro: al volver a publicarlo pierde las visitas y la posición en las búsquedas. Mientras una pieza siga a la venta en una cuenta, su anuncio en esa cuenta se actualiza; no se retira y se vuelve a crear.
3. Jordi publica cosas a mano desde Piezalia (lotes, piezas sin referencia). No son nuestras y no se tocan.
4. Cuando Jordi llama a Marta, de soporte, preguntando «¿por qué el elevalunas 8090 no está en mi cuenta 4?», Marta tiene que poder contestarle en el momento, sin leer logs ni entrar al servidor.

## Cómo se usa

- Lo lanza un cron en un servidor nuestro: `import` cada vez que llega un feed nuevo de Rovira y `sync` cada hora. Cuando corre, nadie está mirando la pantalla.
- Con el catálogo entero de Rovira, una `sync` tarda entre diez minutos y dos horas, porque Piezalia limita las llamadas.

## Qué te damos

- `portal-sim/`: el simulador de Piezalia, con su `Dockerfile`. No lo modifiques: lo usaremos tal cual.
- `portal-sim/openapi.yaml`: la especificación de la API de Piezalia. Con el simulador arrancado la tienes también en `/docs`.
- `accounts.csv`: las cuatro cuentas de Rovira en Piezalia y su token.
- `data/feed_dia1.csv` y `data/feed_dia2.csv`: los feeds de Rovira de dos días seguidos, en ese orden. Columnas: `product_id`, `title`, `price` (decimal con punto) y `store_id` (las cuentas donde se publica, separadas por `|`).

## Qué tienes que construir

Una aplicación en Python, con tres comandos:

- `import <feed>`: lee el feed y guarda el catálogo en Postgres.
- `sync`: deja cada cuenta de Piezalia como dice el catálogo.
- `status <product_id>`: lo que Marta necesita saber de esa pieza.

Cómo lo organizas, qué tecnologías usas, qué guardas y cómo lo compruebas lo decides tú. Se valorarán las buenas prácticas.

## Qué entregar

- Un repositorio git (enlace a GitHub, o un zip con la carpeta `.git`).
- `README.md`: lo que haga falta para arrancarlo.
- `DECISIONES.md`, de una página como mucho: qué has decidido y por qué, sobre todo en lo que este enunciado no dice. Si algo no te cuadra, decide tú y explícalo ahí.
- Si has usado IA, la conversación exportada.
