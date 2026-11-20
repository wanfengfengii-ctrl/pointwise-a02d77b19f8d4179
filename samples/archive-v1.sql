PRAGMA user_version=1;
CREATE TABLE records(
    id TEXT PRIMARY KEY NOT NULL,
    kind_uri TEXT NOT NULL,
    kind_local TEXT NOT NULL,
    href TEXT NOT NULL
);
CREATE TABLE attachments(
    name TEXT PRIMARY KEY NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL
);
CREATE TABLE attachment_chunks(
    name TEXT NOT NULL REFERENCES attachments(name) ON DELETE CASCADE,
    part INTEGER NOT NULL CHECK(part >= 0),
    data BLOB NOT NULL,
    PRIMARY KEY(name, part)
);
INSERT INTO records VALUES('historical-1','','Sale','https://vendor.example/archive/1');
INSERT INTO attachments VALUES('supplier-note',12,'4dc710920edb0925bb5666b6636eb86d41146f2f2d5ab0ddea4ec3202d3e312d');
INSERT INTO attachment_chunks VALUES('supplier-note',0,X'76656E');
INSERT INTO attachment_chunks VALUES('supplier-note',1,X'646F722D6E6F74650A');
