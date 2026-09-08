// APB4 completer that raises PSLVERR during its wait state -- deliberately broken.
//
// Identical to apb_completer_ok.v except for the PSLVERR cone:
//   PSLVERR = access & ~PREADY & ERR_IN
// which asserts the error response one cycle before the transfer completes.
// PSLVERR is only sampled when PSEL, PENABLE and PREADY are all high, so this
// reports an error for a transfer that has not finished, and a requester that
// samples it on the completing edge sees no error at all.
//
// Breaks: apb/completer/slverr_requires_ready.

module apb_completer_broken_slverr (
    PCLK,
    PRESETn,
    PSEL,
    PENABLE,
    PWRITE,
    PADDR_0,
    PWDATA_0,
    ERR_IN,
    PREADY,
    PRDATA_0,
    PSLVERR
);
    input PCLK;
    input PRESETn;
    input PSEL;
    input PENABLE;
    input PWRITE;
    input PADDR_0;
    input PWDATA_0;
    input ERR_IN;
    output PREADY;
    output PRDATA_0;
    output PSLVERR;

    wire PCLK;
    wire PRESETn;
    wire PSEL;
    wire PENABLE;
    wire PWRITE;
    wire PADDR_0;
    wire PWDATA_0;
    wire ERR_IN;
    wire PREADY;
    wire PRDATA_0;
    wire PSLVERR;

    wire vdd;
    wire rst;
    wire access;
    wire w_q;
    wire w_n;
    wire w_d;
    wire nready;
    wire dat;

    VCC vcc_inst (
        .O(vdd)
    );
    INV rst_inv (
        .I(PRESETn),
        .O(rst)
    );
    AND2 access_and (
        .I0(PSEL),
        .I1(PENABLE),
        .O(access)
    );
    INV w_inv (
        .I(w_q),
        .O(w_n)
    );
    AND2 w_next_and (
        .I0(access),
        .I1(w_n),
        .O(w_d)
    );
    FFR w_reg (
        .C(PCLK),
        .CE(vdd),
        .D(w_d),
        .R(rst),
        .Q(w_q)
    );
    AND2 ready_and (
        .I0(access),
        .I1(w_q),
        .O(PREADY)
    );
    INV nready_inv (
        .I(PREADY),
        .O(nready)
    );
    AND3 slverr_and (
        .I0(access),
        .I1(nready),
        .I2(ERR_IN),
        .O(PSLVERR)
    );
    AND3 dat_and (
        .I0(PWRITE),
        .I1(PADDR_0),
        .I2(PWDATA_0),
        .O(dat)
    );
    XOR rdata_xor (
        .I0(w_q),
        .I1(dat),
        .O(PRDATA_0)
    );
endmodule
