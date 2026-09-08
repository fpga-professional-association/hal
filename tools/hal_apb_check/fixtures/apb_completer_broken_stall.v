// APB4 completer whose wait states never end -- deliberately broken.
//
// Identical to apb_completer_ok.v except that PREADY is additionally gated by a
// primary input:
//   PREADY = access & w_q & STALL_N
// so an environment that holds STALL_N low stretches the ACCESS phase without
// limit. APB itself sets no upper bound on wait states, which is exactly why the
// checker's liveness obligation is a *bounded* one against the mapping's
// declared max_wait_states budget -- and this fixture blows through it.
//
// Breaks: apb/completer/ready_within_bound.

module apb_completer_broken_stall (
    PCLK,
    PRESETn,
    PSEL,
    PENABLE,
    PWRITE,
    PADDR_0,
    PWDATA_0,
    ERR_IN,
    STALL_N,
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
    input STALL_N;
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
    wire STALL_N;
    wire PREADY;
    wire PRDATA_0;
    wire PSLVERR;

    wire vdd;
    wire rst;
    wire access;
    wire w_q;
    wire w_n;
    wire w_d;
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
    AND3 ready_and (
        .I0(access),
        .I1(w_q),
        .I2(STALL_N),
        .O(PREADY)
    );
    AND2 slverr_and (
        .I0(PREADY),
        .I1(ERR_IN),
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
