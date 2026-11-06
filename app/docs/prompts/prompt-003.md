Now we'll tackle the `sync` command

<!-- Let's continue with a new menu item, the corresponding command and the underlying core functionality. -->
<!---->
<!-- First, I'll give you some instructions and guidelines here and I want you to prepare a structured and detailed plan, which I'll discuss and approve. -->
<!---->
<!-- We are going to implement the `import <feed>` functionality. -->
<!---->
<!-- ### Prerequisites -->
<!---->
<!-- - Establish a connection to the database if we don't have one already. (with init_db, which will create it if it doesn't exist) -->
<!-- - A csv file containing the catalog of the day. -->
<!---->
<!-- ### Conditions -->
<!---->
<!-- - Opposed to uploading to piezalia, this process isn't bound to an external provider, therefore is more likely than not that it finishes updating the database before the new cronjob. Anyway, it should be contemplated anyway, and the atomicity of the database is already regarded (different cronjob/process means the possibility of overlapping calls to database...) -->
<!---->
<!-- ### Functionality -->
<!---->
<!-- Provided that the csv files and the database can increase overtime (for a particular like Rovira might not be necessary, but for a larger company it's better suited), managing this rows individually by iterating can be grossly time-consuming, therefore I thought the best idea could be adding a temporal table with the ingested data of the csb (staging table), and then perform bulk operations using SQL set-based operations. -->
<!---->
<!-- This operations must run on conflicts (feed /= db), and this is how they need to be addressed; -->
<!---->
<!-- 1. Exists in feed, but not in database -> add row, put it unlisted. -->
<!-- 2. Exists in db, but not in feed -> if status=listed, change it to sold if unlisted just remove. -->
<!-- 3. Exists in both but different data, we'll have to add a new status "modified" to make afterward sync operations notice the changes. Then put that row to modified. -->
<!---->
<!-- Also before adding new rows or performing any operation, revise the data is correct and implement roll-back policy if error (COMMIT;) -->
<!---->
<!-- Also make modifications clear in crated_at and modified_at fields (currently not set in the schema). -->
